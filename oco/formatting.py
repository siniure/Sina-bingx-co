"""Persian mobile report, always branded as offline research."""
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from zoneinfo import ZoneInfo

PERSIAN = {"long": "صعودی", "short": "نزولی", "neutral": "خنثی/ناسازگار"}


def report_fa(result: dict) -> str:
    lines = ["کنترل مجدد و بازبینی؛ توافق ما؛ قابل کپی", "",
             "**اجرای آزمایشی آفلاین — سیگنال زنده یا سفارش واقعی نیست.**", ""]
    if result.get("as_of"):
        time = datetime.fromisoformat(result["as_of"]).astimezone(ZoneInfo("Europe/Istanbul"))
        lines += [f"زمان دادهٔ بازپخش به وقت استانبول: {time:%Y-%m-%d %H:%M:%S %z}", ""]
    signal = result.get("signal")
    lines += ["طرح پژوهشی مشروط؛ نیازمند بررسی منبع و ریسک حساب." if signal else "فعلاً ورود ندارد.",
              f"وضعیت: {result['status']}؛ علت: {result.get('reason', 'unknown')}", ""]
    if result.get("timeframes"):
        lines += ["| تایم‌فریم | جهت | حمایت‌ها از نزدیک به دور | مقاومت‌ها از نزدیک به دور |",
                  "|---|---|---|---|"]
        for tf, item in result["timeframes"].items():
            lines.append(f"| {tf} | {PERSIAN[item['direction']]} | {', '.join(item['supports']) or 'ناموجود'} | {', '.join(item['resistances']) or 'ناموجود'} |")
        lines.append("")
        lines += ["| تایم‌فریم | MACD / Signal / Histogram | ATR14 RMA | RSI14 / RSI24 |",
                  "|---|---|---|---|"]
        for tf, item in result["timeframes"].items():
            lines.append(f"| {tf} | {item['macd_line']:.4f} / {item['macd_signal']:.4f} / {item['macd_histogram']:.4f} | {item['atr14_rma']:.4f} | {item['rsi14']:.2f} / {item['rsi24']:.2f} |")
        lines += ["", "نمایش جدول گرد شده است؛ محاسبات از مقدار کامل استفاده می‌کنند.", ""]
    if signal:
        risk = signal["risk"]
        rate = Decimal(signal["costs"]["worst_net_rr"])
        if rate >= 4:
            rate_label = "فوق‌العاده"
        elif rate >= 3:
            rate_label = "عالی"
        elif rate >= 2:
            rate_label = "خوب"
        else:
            rate_label = "متوسط"
        displayed_rate = rate.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        lines += [f"جهت پژوهشی: {PERSIAN[signal['direction']]}؛ شناسه: {signal['id']}",
                  f"انقضای طرح: {datetime.fromisoformat(signal['expires_at']).astimezone(ZoneInfo('Europe/Istanbul')).isoformat()}", "",
                  f"مدت نگهداری فرضی پس از Fill حداکثر {signal.get('maximum_hold_minutes', 120)} دقیقه است.", "",
                  f"⭐ **Rate {displayed_rate} — {rate_label}**", "",
                  "| کنترل | نتیجهٔ آزمایشی |", "|---|---|",
                  f"| نسبت خالص در بدترین انتهای ورود | {signal['costs']['worst_net_rr']} |",
                  f"| وضعیت ریسک حساب | {risk['status']} |",
                  f"| درصد زیان برنامه‌ریزی‌شده | {risk.get('risk_percent', 'ناموجود')} |",
                  f"| مبلغ زیان / USDT | {risk.get('risk_loss_usdt', 'ناموجود')} |",
                  f"| امتیاز شدت ریسک | {risk.get('risk_intensity_score', 'ناموجود')} |",
                  f"| ردهٔ شدت ریسک | {risk.get('risk_intensity_label_fa', 'ناموجود')} |",
                  f"| Equity و زمان مبنا | {risk.get('equity', 'ناموجود')} / {risk.get('equity_at', 'ناموجود')} |",
                  f"| مجموع ریسک حساب و طرح / USDT | {risk.get('aggregate_risk_usdt', 'ناموجود')} |",
                  f"| قیمت جاری بازپخش؛ متمایز از Entry | {result['quote']['price']} |",
                  f"| حجم فرضی / BTC | {risk.get('quantity') or 'ناموجود'} |", "",
                  "❗❗❗ **شرط فعال‌سازی** ❗❗❗", "",
                  f"قیمت در بازهٔ **{signal['entry_range']['min']}–{signal['entry_range']['max']}** باقی بماند.", ""]
        lines += [f"- {condition}" for condition in [
            "جهت پنج تایم‌فریم بالاتر همسو بماند.", "شکست و بازآزمایی با کندل بسته تأیید شده باشد.",
            "قیمت جاری داخل محدودهٔ ورود بماند.", "از زمان تأیید، TP یا SL لمس نشده باشد.",
            "تازگی داده، هزینه‌ها و ریسک حساب پیش از کاربرد واقعی دوباره کنترل شوند."]]
        lines += ["", "⛔⛔ **شرط ابطال پلن** ⛔⛔", "",
                  f"رسیدن قیمت به **{signal['sl']}** یا **{signal['tp']}** پیش از Fill، یا پایان مهلت ورود، طرح را منقضی می‌کند."]
        for label, key in (("🟦 **Entry**", "entry_reference"), ("🟥 **SL**", "sl"), ("🟩 **TP**", "tp")):
            lines += ["", label, "", "```", signal[key], "```"]
        lines += ["", "لمس قیمت، مدرک پرشدن سفارش یا سود و زیان واقعی نیست."]
    return "\n".join(lines)+"\n"
