# قرارداد ورودی بازپخش schema_version=2

این قرارداد، رابط آینده دریافت داده را روشن می‌کند؛ واردکردن JSON جای اتصال واقعی صرافی نیست. در نسخه۰٫۳ فقط data_mode برابر synthetic یا recorded_replay پذیرفته می‌شود. live حتی با فیلد source_verified=true رد می‌شود. نسخه قرارداد با schema_version قدیمی core.py متفاوت است؛ آن هسته برای سازگاری حفظ شده است.

مبنای نمونه کامل و تکرارپذیر: تابع oco.demo.synthetic_snapshot(). قیمت‌ها، حساب و نرخ‌های آن ساختگی‌اند. اجرا با scripts/run_demo.py نمونه را می‌سازد.

| فیلد | معنی و کنترل |
|---|---|
| schema_version | عدد صحیح۲ |
| provider/product/symbol/price_kind | دقیقاً BingX / standard_futures / BTCUSDT / index_price |
| data_mode | synthetic یا recorded_replay |
| source_id | شناسه یکسان در قیمت و تمام تاریخچه‌ها؛ احراز اصالت مستقل نیست |
| as_of | ساعت بازپخش با timezone؛ worker ساعت واقعی را هم برای تازگی کنترل می‌کند |
| quote | هویت محصول و منبع، price به‌صورت decimal string، event_at و observed_at با timezone، timestamp_kind=exchange_event |
| histories | دقیقاً شش کلید5m،15m،1h،4h،12h،1d |
| هر history | هویت کامل، source_id،origin_at واقعی صرافی،observed_at،bars |
| هر bar | open_at،close_at انحصاری،closed=true،open/high/low/close،volume اختیاری؛ بدون شکاف یا مرتب‌سازی پنهان |
| quote_path | تمام مشاهدات بازپخش از مرز تأیید تا quote؛ زمان، منبع، sequence پیوسته و قیمت |
| costs | هویت محصول،verified_at،expires_at حداکثر یک ساعت،evidence،entry_fee_rate،exit_fee_rate،execution_reserve_per_unit،funding_reserve_per_unit؛ نرخ‌ها کسری‌اند |
| account اختیاری | هویت،as_of،equity،available_margin،coverage_complete،positions_evidence،entry_orders_evidence،exposures |
| هر exposure | id یکتا،kind=position/entry_order،stop_verified،additional_loss_to_stop_usdt بر مبنای Equity فعلی و شامل هزینه باقی‌مانده |

تمام فیلدهای پولی و قیمت در لایه ریسک decimal string یا integer هستند؛ float و boolean پذیرفته نمی‌شوند. داده کندل به‌علت محاسبات اندیکاتور به float تبدیل و محدودیت finite کنترل می‌شود. محاسبات حجم/کارمزد با Decimal‌اند. حداکثر فایل۳۲MiB است؛ کلید تکراری JSON و NaN/Infinity رد می‌شوند.

هیچ متنی از payload، پرامپت، مدرک منبع یا پیام مدل به رابط کنترل وصل نیست. فرمان‌های کنترل فقط از CLI محلی مورد اعتماد می‌آیند. API Key یا Secret در این فایل قرار نمی‌گیرد.

account و costs واردشده همچنان ادعاهای ورودی‌اند. کلاینت Standard موجود، این snapshot ریسک کامل را از سه پاسخ حساب جعل نمی‌کند؛ به‌خصوص تاریخچه سفارش با تمام سفارش‌های ورودی فعال برابر فرض نشده و balance بدون بررسی، Equity نامیده نمی‌شود. تا اتصال و پوشش واقعی، حجم صرفاً فرضی یا ناموجود است.
