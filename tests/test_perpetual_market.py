"""Offline candidate-contract tests. No test authenticates BingX live data."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError
import unittest

from oco.perpetual_market import (
    ALLOWED_PATHS, BOOK_PATH, KLINES_PATH, MAX_RESPONSE_BYTES,
    PRICE_PATH, TIME_PATH, TIMEFRAMES, BingXPublicClient, MarketError, _NoRedirects,
)

UTC = timezone.utc


class Fixture:
    def __init__(self):
        self.now = datetime(2026, 11, 11, 0, 15, 10, tzinfo=UTC)
        self.calls = []
        self.hook = None

    def clock(self):
        return self.now

    def transport(self, path, params):
        self.calls.append((path, dict(params)))
        now_ms = int(self.now.timestamp() * 1000)
        if path == TIME_PATH:
            data = {"serverTime": now_ms}
        elif path == PRICE_PATH:
            data = {"symbol": "BTC-USDT", "price": "73600.10", "time": now_ms}
        elif path == BOOK_PATH:
            data = {"symbol": "BTC-USDT", "bidPrice": "73599.90", "askPrice": "73600.20", "time": now_ms}
        elif path == KLINES_PATH:
            interval = TIMEFRAMES[params["interval"]] * 1000
            latest = params.get("endTime", now_ms) // interval * interval
            # Reverse order and include the current open bar on initial fetch.
            data = [{"time": latest-index*interval, "open": "70000", "high": "70020",
                     "low": "69980", "close": "70005", "volume": "10.2"}
                    for index in range(params["limit"])]
        else:
            raise AssertionError("unexpected fixture route")
        response = {"code": 0, "data": data}
        return response if self.hook is None else self.hook(path, params, response)

    def client(self):
        return BingXPublicClient(self.transport, self.clock)


class PerpetualMarketTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()

    def test_snapshot_is_canonical_perpetual_with_raw_api_symbol(self):
        result = self.fixture.client().collect()
        self.assertEqual(result["symbol"], "BTCUSDT")
        self.assertEqual(result["source_symbol"], "BTC-USDT")
        self.assertEqual(result["product"], "perpetual_futures")
        self.assertEqual(result["price_kind"], "last_price")
        self.assertFalse(result["live_schema_verified"])
        self.assertEqual(set(result["frames"]), set(TIMEFRAMES))
        self.assertEqual(result["quote"]["bid"], "73599.90")
        for name, rows in result["frames"].items():
            self.assertEqual(len(rows), 400)
            self.assertTrue(all(row["closed"] for row in rows))
            ends = [datetime.fromisoformat(row["close_at"]) for row in rows]
            self.assertLessEqual(ends[-1], self.fixture.now)
            self.assertEqual(ends, sorted(ends))
            self.assertEqual(int(ends[-1].timestamp()) % TIMEFRAMES[name], 0)
        for path, params in self.fixture.calls:
            self.assertIn(path, ALLOWED_PATHS)
            if "symbol" in params:
                self.assertEqual(params["symbol"], "BTC-USDT")

    def test_open_bar_is_excluded_and_future_bar_is_rejected(self):
        client = self.fixture.client()
        result = client.collect()
        first_open = int(self.fixture.now.timestamp()) // 300 * 300
        self.assertEqual(result["frames"]["5m"][-1]["close_at"],
                         datetime.fromtimestamp(first_open, UTC).isoformat())

        def future(path, params, response):
            if path == KLINES_PATH:
                response["data"][0]["time"] += TIMEFRAMES[params["interval"]] * 1000
            return response

        self.fixture.hook = future
        with self.assertRaisesRegex(MarketError, "future"):
            self.fixture.client().collect()

    def test_duplicate_candles_are_rejected_before_sorting(self):
        def duplicate(path, params, response):
            if path == KLINES_PATH:
                response["data"][2] = dict(response["data"][1])
            return response

        self.fixture.hook = duplicate
        with self.assertRaisesRegex(MarketError, "duplicate"):
            self.fixture.client().collect()

    def test_gap_is_not_filled_or_hidden_by_sorting(self):
        def gap(path, params, response):
            if path == KLINES_PATH:
                interval = TIMEFRAMES[params["interval"]] * 1000
                response["data"][30]["time"] = response["data"][-1]["time"] - interval
            return response

        self.fixture.hook = gap
        with self.assertRaisesRegex(MarketError, "gap"):
            self.fixture.client().collect()

    def test_history_must_include_the_latest_expected_close(self):
        def stale_history(path, params, response):
            if path == KLINES_PATH:
                interval = TIMEFRAMES[params["interval"]] * 1000
                response["data"].pop(1)
                oldest = dict(response["data"][-1])
                oldest["time"] -= interval
                response["data"].append(oldest)
            return response

        self.fixture.hook = stale_history
        with self.assertRaisesRegex(MarketError, "latest expected"):
            self.fixture.client().collect()

    def test_missing_volume_stays_none_with_an_explicit_warning(self):
        def missing_volume(path, params, response):
            if path == KLINES_PATH:
                for row in response["data"]:
                    row.pop("volume")
            return response

        self.fixture.hook = missing_volume
        result = self.fixture.client().collect()
        self.assertIsNone(result["frames"]["5m"][-1]["volume"])
        self.assertEqual(result["volume"]["missing_counts"]["5m"], 400)
        self.assertIn("Missing candle volumes are preserved as null.", result["warnings"])

    def test_unsupported_twelve_hour_data_is_not_synthesized(self):
        def unsupported(path, params, response):
            if path == KLINES_PATH and params["interval"] == "12h":
                return {"code": 80001, "msg": "unsupported interval"}
            return response

        self.fixture.hook = unsupported
        with self.assertRaisesRegex(MarketError, "rejected"):
            self.fixture.client().collect()

    def test_stale_or_future_price_and_book_quotes_are_rejected(self):
        for route, offset in ((PRICE_PATH, -16000), (BOOK_PATH, -16000),
                              (PRICE_PATH, 3000), (BOOK_PATH, 3000)):
            with self.subTest(route=route, offset=offset):
                def change_time(path, params, response):
                    if path == route:
                        response["data"]["time"] += offset
                    return response

                self.fixture.hook = change_time
                with self.assertRaisesRegex(MarketError, "stale or in the future"):
                    self.fixture.client().collect()

    def test_server_clock_drift_and_missing_server_time_are_rejected(self):
        for mode in ("drift", "missing"):
            with self.subTest(mode=mode):
                def change(path, params, response):
                    if path == TIME_PATH:
                        if mode == "drift":
                            response["data"]["serverTime"] -= 6000
                        else:
                            response["data"].pop("serverTime")
                    return response

                self.fixture.hook = change
                with self.assertRaises(MarketError):
                    self.fixture.client().collect()

    def test_price_and_book_exchange_timestamps_are_required(self):
        for route in (PRICE_PATH, BOOK_PATH):
            with self.subTest(route=route):
                def remove_time(path, params, response):
                    if path == route:
                        response["data"].pop("time")
                    return response

                self.fixture.hook = remove_time
                with self.assertRaisesRegex(MarketError, "exchange time"):
                    self.fixture.client().collect()

    def test_api_code_schema_and_market_identity_are_strict(self):
        responses = [[], {"code": True, "data": {}}, {"code": Decimal("0"), "data": {}},
                     {"code": 0}, {"code": 0, "product": "standard_futures", "data": {}},
                     {"code": 0, "provider": "Other", "data": {}}]
        for response in responses:
            with self.subTest(response=response):
                client = BingXPublicClient(lambda path, params: response, self.fixture.clock)
                with self.assertRaises(MarketError):
                    client.probe()

    def test_wrong_symbol_and_standard_product_in_quote_or_bars_are_rejected(self):
        for route, field, value in ((PRICE_PATH, "symbol", "BTCUSDT"),
                                    (BOOK_PATH, "product", "standard_futures"),
                                    (KLINES_PATH, "product", "standard_futures")):
            with self.subTest(route=route, field=field):
                def wrong_identity(path, params, response):
                    if path == route:
                        target = response["data"][0] if path == KLINES_PATH else response["data"]
                        target[field] = value
                    return response

                self.fixture.hook = wrong_identity
                with self.assertRaises(MarketError):
                    self.fixture.client().collect()

    def test_nonfinite_prices_crossed_book_and_invalid_ohlc_are_rejected(self):
        cases = ((PRICE_PATH, "price", "NaN"), (PRICE_PATH, "price", "-1"),
                 (BOOK_PATH, "bidPrice", "Infinity"), (BOOK_PATH, "bidPrice", "73601"),
                 (KLINES_PATH, "low", "90000"), (KLINES_PATH, "volume", "-1"))
        for route, field, value in cases:
            with self.subTest(route=route, field=field, value=value):
                def invalid(path, params, response):
                    if path == route:
                        target = response["data"][1] if path == KLINES_PATH else response["data"]
                        target[field] = value
                    return response

                self.fixture.hook = invalid
                with self.assertRaises(MarketError):
                    self.fixture.client().collect()

    def test_history_cache_is_bounded_and_cannot_be_mutated_by_callers(self):
        client = self.fixture.client()
        result = client.collect()
        result["frames"]["5m"][-1]["close"] = "99999"
        self.fixture.now += timedelta(seconds=5)
        next_result = client.collect(symbol="BTCUSDT")
        self.assertEqual(next_result["frames"]["5m"][-1]["close"], "70005")
        self.assertEqual(sum(path == KLINES_PATH for path, _ in self.fixture.calls), 6)
        self.fixture.now += timedelta(minutes=5)
        client.collect()
        self.assertEqual(sum(path == KLINES_PATH for path, _ in self.fixture.calls), 7)
        self.assertEqual(len(client._history_cache), 6)

    def test_a_boundary_crossed_during_quote_fetch_blocks_the_snapshot(self):
        self.fixture.now = datetime(2026, 11, 11, 0, 19, 59, 700000, tzinfo=UTC)

        def cross_boundary(path, params, response):
            if path == BOOK_PATH:
                self.fixture.now += timedelta(milliseconds=400)
                response["data"]["time"] = int(self.fixture.now.timestamp() * 1000)
            return response

        self.fixture.hook = cross_boundary
        with self.assertRaisesRegex(MarketError, "latest expected"):
            self.fixture.client().collect()

    def test_maximum_warmup_uses_a_bounded_extra_page_for_the_open_bar(self):
        result = self.fixture.client().collect(warmup=1000)
        self.assertTrue(all(len(rows) == 1000 for rows in result["frames"].values()))
        calls = [params for path, params in self.fixture.calls if path == KLINES_PATH]
        self.assertEqual(len(calls), 12)
        self.assertTrue(all(params["limit"] <= 1000 for params in calls))

    def test_invalid_scope_and_warmup_are_rejected_without_network(self):
        client = self.fixture.client()
        for value in (399, 1001, True, 400.0):
            with self.assertRaises(MarketError):
                client.collect(warmup=value)
        with self.assertRaises(MarketError):
            client.collect(symbol="ETH-USDT")
        self.assertEqual(self.fixture.calls, [])

    def test_probe_never_claims_schema_verification_or_loads_histories(self):
        result = self.fixture.client().probe()
        self.assertFalse(result["verified"])
        self.assertFalse(result["live_schema_verified"])
        self.assertEqual(result["symbol"], "BTCUSDT")
        self.assertEqual(result["source_symbol"], "BTC-USDT")
        self.assertEqual([path for path, _ in self.fixture.calls], [TIME_PATH, PRICE_PATH])

    def test_transport_exception_does_not_leak_its_message(self):
        def fail(path, params):
            raise RuntimeError("private-token-value")

        with self.assertRaises(MarketError) as caught:
            BingXPublicClient(fail, self.fixture.clock).probe()
        self.assertNotIn("private-token", str(caught.exception))

    def test_http_errors_and_redirects_fail_without_reading_their_bodies(self):
        error = HTTPError("https://open-api.bingx.com", 403, "secret response", {}, BytesIO(b"secret-body"))
        with patch("oco.perpetual_market.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(MarketError, "public API HTTP 403") as caught:
                BingXPublicClient().probe()
            self.assertNotIn("secret", str(caught.exception))
        with self.assertRaisesRegex(MarketError, "redirect"):
            _NoRedirects().redirect_request(None, None, 302, "", {}, "https://other.example")
        with self.assertRaisesRegex(MarketError, "allowlist"):
            BingXPublicClient._http_get("/openApi/swap/v2/trade/order", {})

    def test_http_response_size_and_json_validation_are_bounded(self):
        class Response:
            status = 200

            def __init__(self, payload, length=None):
                self.payload = payload
                self.headers = {} if length is None else {"Content-Length": str(length)}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                return self.payload[:size]

        responses = [Response(b"", MAX_RESPONSE_BYTES+1),
                     Response(b"x"*(MAX_RESPONSE_BYTES+1)), Response(b"not json"),
                     Response(b'{"code": 0, "data": NaN}')]
        for response in responses:
            with self.subTest(length=response.headers.get("Content-Length")):
                with patch("oco.perpetual_market.build_opener") as opener:
                    opener.return_value.open.return_value = response
                    with self.assertRaises(MarketError):
                        BingXPublicClient().probe()


if __name__ == "__main__":
    unittest.main()
