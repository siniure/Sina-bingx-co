# منابع و نتیجه بررسی اتصال — ۲ نوامبر ۲۰۲۶

## BingX Standard

مرجع رسمی فعلی، سه مسیر GET برای پوزیشن‌ها، تاریخچه سفارش‌ها و موجودی Standard را معرفی می‌کند: allPosition،allOrders و balance زیر /openApi/contract/v1/. کلاینت محدود همین سه مسیر ساخته شده و فقط با انتقال ساختگی آزموده شده است. وجود این مسیرها، دسترسی عملی کلید سینا یا پوشش تمام سفارش‌های فعال/استاپ‌ها را ثابت نمی‌کند. [مرجع رسمی Standard](https://github.com/BingX-API/api-ai-skills/blob/main/skills/standard-trade/api-reference.md)

امضای درخواست از پارامترهای مرتب‌شده خام با HMAC-SHA256 ساخته می‌شود؛ هدرهای مستند و recvWindow حداکثر۵۰۰۰ استفاده می‌شوند. کلاینت redirect را رد می‌کند و هیچ مسیر نوشتنی ندارد. [احراز هویت رسمی](https://github.com/BingX-API/api-ai-skills/blob/main/skills/references/authentication.md)

نسخه قدیمی‌تر مستندات Standard نیز فقط پرس‌وجوهای حساب را توضیح می‌دهد؛ توضیح قدیمی آزمایش داخلی در آن نباید به وضعیت قطعی فعلی تعمیم داده شود. [مخزن رسمی قدیمی](https://github.com/BingX-API/BingX-Standard-Contract-doc/blob/main/REST%20API.md)

صفحه نامزد Standard در بازیابی متنی، منو و پوسته رابط برگرداند. محصول انتخاب‌شده، Index Price زنده، زمان رویداد و OHLC استاندارد از آن اثبات نشد. عدد عنوان صفحه به‌عنوان قیمت جاری استفاده نشده است. API کندل swap نیز جایگزین نشده است. [صفحه نامزد](https://bingx.com/en/futures/forward/BTCUSDT)

## Render

طبق پرونده ارائه‌شده توسط کاربر، حساب Render متصل است. در کشف جدید همین گفت‌وگو هیچ فرمان واقعی Render موجود نبود؛ CLI و متغیر RENDER_API_KEY نیز آماده نبودند. هیچ workspace خوانده یا عوض نشده و منبعی ساخته نشده است. این وضعیت به‌معنی لغو اتصال حساب یا فقدان مجوز کاربر نیست. [مرجع MCP](https://render.com/docs/mcp-server)

مستندات جاری از مسیر وب خوانده شد؛ بازیابی Markdown با نوع محتوای پشتیبانی‌نشده روبه‌رو شد و HTML جایگزین شد. این دسترسی به اسناد عمومی، دسترسی به حساب Render ایجاد نمی‌کند.

worker برای فرایند پیوسته بدون ورودی HTTP انتخاب شده است. [worker](https://render.com/docs/background-workers)، [انواع سرویس](https://render.com/docs/service-types)

قالب نمونه از runtime Python، plan پیشنهادی 0.5c-512mb و autoDeployTrigger: off استفاده می‌کند. فایل تنها نمونه است؛ نام سرویس، workspace، مخزن و هزینه واقعی باید قبل از ساخت بررسی شوند. مبلغ هزینه در این نوبت از جدول قابل‌اتکایی استخراج نشده و قیمت حدسی ارائه نشده است. [Blueprint](https://render.com/docs/blueprint-spec)، [compute plans](https://render.com/docs/compute-plans)، [قیمت](https://render.com/pricing)

SQLite و فایل‌های runtime باید در دیسک ماندگار باشند؛ در نمونه /var/data انتخاب شده است. سرویس دیسک‌دار تک‌نمونه است و هنگام استقرار وقفه دارد؛ پیش از بازگشت باید تاریخچه و پیوستگی فید دوباره کنترل شوند. سلامت worker باید از لاگ/heartbeat و کار واقعی تأیید شود؛ healthCheckPath برای این نوع سرویس درج نشده است. [دیسک‌ها](https://render.com/docs/disks)، [deploy](https://render.com/docs/deploys)، [health checks](https://render.com/docs/health-checks)

## حدود آزمون

هیچ درخواست امضاشده به حساب، ثبت سفارش، ارسال تلگرام یا استقرار اجرا نشده است. کلاینت فقط‌خواندنی از worker بازپخش فراخوانی نمی‌شود. انتخاب مقصد اعلان همچنان با سینا است.
