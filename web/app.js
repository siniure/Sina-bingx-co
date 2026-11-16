(function () {
  'use strict';
  const model = {
    validNumber: value => typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value) && Number.isFinite(Number(value)) && Number(value)>0,
    perpetual: value => value?.provider === 'BingX' && value.product === 'perpetual_futures' &&
      value.symbol === 'BTCUSDT' && value.price_kind === 'last_price',
    freshPerpetualQuote(market, elapsed=0) {
      return model.perpetual(market) && market.source_symbol === 'BTC-USDT' &&
        market.transport === 'websocket' && model.validNumber(market.price) &&
        typeof market.event_at === 'string' && Number.isFinite(Date.parse(market.event_at)) &&
        Number.isFinite(market.age_seconds) && market.age_seconds >= -2 &&
        Math.max(0, market.age_seconds) + elapsed < 15;
    },
    serverURL(value) {
      const parsed = new URL(value);
      if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.search || parsed.hash || parsed.pathname !== '/') throw new Error('HTTPS_REQUIRED');
      return parsed.origin + '/';
    },
    safeEvaluation(input) {
      if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('INVALID_REPORT');
      const result = {...input, live: false};
      if (result.data_mode === 'live_observation' && (!model.perpetual(result) || result.signal)) throw new Error('SOURCE_IDENTITY_REQUIRED');
      if (result.signal) {
        const standard = result.provider === 'BingX' && result.product === 'standard_futures' &&
          result.symbol === 'BTCUSDT' && result.price_kind === 'index_price';
        if (!(standard || model.perpetual(result)) || !['synthetic','recorded_replay'].includes(result.data_mode)) throw new Error('SOURCE_IDENTITY_REQUIRED');
        const signal = result.signal;
        if (!['long', 'short'].includes(signal.direction) || !['entry_reference','sl','tp'].every(k => model.validNumber(signal[k]))) throw new Error('INVALID_PRICES');
        const e=Number(signal.entry_reference), s=Number(signal.sl), t=Number(signal.tp);
        if (![e,s,t].every(Number.isFinite) || !(signal.direction==='long' ? s<e&&e<t : t<e&&e<s)) throw new Error('INVALID_GEOMETRY');
      }
      return result;
    }
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = model;
  if (typeof document === 'undefined') return;
  const $ = id => document.getElementById(id);
  let token = '', timer = null, quoteTimer = null, sessionGeneration = 0, refreshSequence = 0;
  const isServer = location.protocol === 'https:' || (location.protocol === 'http:' && ['localhost','127.0.0.1','[::1]'].includes(location.hostname));
  function feedback(text, error=false) { $('feedback').textContent=text; $('feedback').classList.toggle('error',error); }
  function istanbul(iso) {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? 'ناموجود' : new Intl.DateTimeFormat('fa-IR',{timeZone:'Europe/Istanbul',dateStyle:'short',timeStyle:'medium'}).format(date);
  }
  function element(tag, text, className='') { const item=document.createElement(tag); item.textContent=text; if(className) item.className=className; return item; }
  async function api(path, method='GET') {
    const response=await fetch(path,{method,headers:{Authorization:'Bearer '+token,...(method==='POST'?{'Content-Type':'application/json'}:{})},body:method==='POST'?'{}':undefined,cache:'no-store',credentials:'omit',redirect:'error',signal:AbortSignal.timeout(method==='POST'?15000:8000)});
    if (!response.ok) {
      const messages={TELEGRAM_UNAUTHORIZED:'توکن بات در تنظیمات امن میزبان معتبر نیست؛ آن را همان‌جا اصلاح کن.',TELEGRAM_POLLING_CONFLICT:'بات به Webhook یا دریافت‌کنندهٔ دیگری متصل است؛ ابتدا اتصال قبلی را بررسی کن.',TELEGRAM_RATE_LIMITED:'تلگرام محدودیت موقت گذاشته؛ کمی بعد دوباره اقدام کن.',TELEGRAM_RECIPIENT_FORBIDDEN:'بات در تلگرام مسدود شده یا اجازهٔ پیام به مقصد را ندارد.',TELEGRAM_NETWORK_UNAVAILABLE:'میزبان فعلاً به تلگرام دسترسی ندارد.'};
      let detail={};try{detail=await response.json();}catch(_){}
      throw new Error(messages[detail.error]||(response.status===401?'کلید دسترسی یا سطح مجوز درست نیست.':response.status===423?'انتشار زنده هنوز مسدود است.':'این عملیات فعلاً در دسترس نیست.'));
    }
    return response.json();
  }
  function renderEvaluation(raw, imported=false) {
    const result=model.safeEvaluation(raw);
    $('decision').textContent=result.signal?'طرح پژوهشی مشروط — ورود زنده فعال نیست':'فعلاً ورود ندارد';
    const closedAt = result.timeframes?.['5m']?.closed_at;
    $('data-label').textContent=imported?'گزارش واردشده':closedAt?'بسته‌شدن کندل ۵ دقیقه‌ای · '+istanbul(closedAt):'داده‌ای برای ارزیابی در دسترس نیست';
    const reasons = {
      MARKET_MONITORING_PAUSED: 'پایش بازار متوقف است.',
      WAITING_FOR_CLOSED_CANDLES: 'قیمت تازه دریافت شد. در انتظار کندل‌های بستهٔ هر شش تایم‌فریم.',
      WAITING_FOR_FRESH_LAST_PRICE: 'در انتظار قیمت تازه از BingX.',
      MULTITIMEFRAME_CONFLICT: 'تایم‌فریم‌های بالاتر هم‌جهت نیستند.',
      FIVE_MINUTE_CONFIRMATION_MISSING: 'کندل ۵ دقیقه‌ای تأیید ورود را نداده است.',
      BREAKOUT_AND_RETEST_MISSING: 'شرط شکست و بازآزمایی کامل نشده است.',
      QUOTE_OUTSIDE_ENTRY_RANGE: 'قیمت زمان ارزیابی خارج از محدودهٔ ورود بود.',
      NO_STRUCTURAL_TARGET_OR_INVALID_PRICE_GEOMETRY: 'هدف و حد ضرر با شروط طرح سازگار نبودند.',
      ZERO_ATR: 'ATR کندل‌ها صفر است.',
    };
    $('reason').textContent=result.signal?'طرح بازپخش پژوهشی':(reasons[result.reason]||'شروط ورود تأیید نشده‌اند.');
    $('levels').replaceChildren();
    $('levels').hidden=!result.signal;
    if(result.signal) for(const [key,label] of [['entry_reference','Entry reference'],['sl','SL trigger/price'],['tp','TP trigger/price']]) {
      const box=element('div','','level'); box.append(element('span',label),element('code',result.signal[key]));
      const button=element('button','کپی عدد'); button.type='button';
      button.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(result.signal[key]);button.textContent='کپی شد';}catch(_){feedback('عدد را از کادر انتخاب و کپی کن.');}});
      box.append(button); $('levels').append(box);
    }
    const items=result.timeframes&&typeof result.timeframes==='object'?Object.entries(result.timeframes):[];
    $('tf-wrap').hidden=!items.length; $('timeframes').replaceChildren();
    for(const [tf,item] of items.slice(0,6)) {
      const row=element('tr','');
      const number=x=>typeof x==='number'&&Number.isFinite(x)?x.toFixed(2):'—';
      for(const text of [tf,({long:'صعودی',short:'نزولی',neutral:'خنثی'})[item?.direction]||'ناموجود',number(item?.rsi14)+' / '+number(item?.rsi24),number(item?.atr14_rma),number(item?.macd_histogram)]) row.append(element('td',text));
      $('timeframes').append(row);
    }
  }
  function clearQuote(message='در انتظار قیمت تازه') {
    clearTimeout(quoteTimer);
    $('price').textContent='—';
    $('price-time').textContent=message;
  }
  function renderStatus(status, elapsed) {
    const market = status.market;
    const paused = market?.state === 'PAUSED';
    $('resume').hidden = !paused;
    $('mode').textContent=paused?'متوقف':market?'پایش پرپچوال':status.state==='REPLAY_RUNNING'?'بازپخش پژوهشی':'متوقف';
    clearQuote(paused?'پایش متوقف است':undefined);
    const freshQuote = model.freshPerpetualQuote(market, elapsed);
    $('mode-detail').textContent=freshQuote
      ? market.closed_candles_ready?'شش تایم‌فریم آماده‌اند':market.history_error?'دریافت کندل‌ها ناموفق بود':'در انتظار کندل‌های بسته'
      : paused?'پایش بازار خاموش است':market?'در انتظار WebSocket':'بازپخش پژوهشی';
    if (freshQuote) {
      $('price').textContent=market.price+' USDT';
      $('price-time').textContent=istanbul(market.event_at);
      const remaining = 15 - Math.max(0, market.age_seconds) - elapsed;
      quoteTimer=setTimeout(()=>{
        clearQuote();$('mode-detail').textContent='قیمت تازه دریافت نشده';
        $('tf-wrap').hidden=true;$('data-label').textContent='در انتظار قیمت تازه';
      }, remaining*1000);
    }
    const telegram=status.telegram||{};
    $('telegram-state').textContent=telegram.channel_delivery_verified?'آزمون کانال موفق بود':telegram.channel_paired?'کانال انتخاب شد':telegram.delivery_verified?'آزمون پیام خصوصی موفق بود':telegram.owner_paired?'حساب متصل است':telegram.configured?'در انتظار اتصال حساب':'متصل نیست';
    $('telegram-detail').textContent=telegram.channel_delivery_verified?'رسید تلگرام تأیید شد':telegram.owner_paired?'برای انتخاب کانال /channel را بفرست':'اتصال حساب باید تأیید شود';
    return freshQuote;
  }
  function renderRecords(records) {
    if(!Array.isArray(records)) throw new Error('INVALID_RECORDS');
    $('record-count').textContent=records.length?records.length+' رکورد پژوهشی':'بدون رکورد'; $('records').replaceChildren();
    if(!records.length) $('records').append(element('p','هنوز پیشنهادی ثبت نشده است.','muted'));
    for(const record of records.slice(0,50)) { const row=element('div','','record');row.append(element('code',String(record.id).slice(0,12)),element('span',String(record.state)),element('span',istanbul(record.expires_at)));$('records').append(row); }
  }
  async function refresh(generation=sessionGeneration) {
    clearTimeout(timer);
    const sequence = ++refreshSequence;
    const started = performance.now();
    const active = () => generation === sessionGeneration && sequence === refreshSequence && token;
    try {
      const [status,evaluation,records]=await Promise.all([api('/api/status'),api('/api/evaluation'),api('/api/records')]);
      if(!active())return;
      const freshQuote = renderStatus(status,(performance.now()-started)/1000);
      const visible = status.market && (!freshQuote || !status.market.closed_candles_ready) && evaluation.timeframes
        ? {...evaluation,timeframes:null,reason:freshQuote?'WAITING_FOR_CLOSED_CANDLES':'WAITING_FOR_FRESH_LAST_PRICE'}
        : evaluation;
      renderEvaluation(visible);renderRecords(records);
      $('connection').textContent='متصل به سرور';$('connection').classList.add('connected');
      $('stop').disabled=false;$('pair').disabled=!status.telegram?.configured;$('test-telegram').disabled=!status.telegram?.owner_paired;
      $('disconnect').hidden=false; feedback('');
    } catch(error) {
      if(!active())return;
      clearQuote();$('mode-detail').textContent='ارتباط با سرور قطع است';
      $('connection').textContent='اتصال قطع یا تأیید نشده';$('connection').classList.remove('connected');
      $('levels').hidden=true;$('tf-wrap').hidden=true;$('data-label').textContent='دریافت تازه تأیید نشده است';$('decision').textContent='فعلاً ورود ندارد';
      $('stop').disabled=true;$('resume').hidden=true;$('pair').disabled=true;$('test-telegram').disabled=true;feedback(error.message||'دریافت وضعیت ناموفق بود.',true);
    } finally {
      if (active()) timer=setTimeout(()=>refresh(generation),4000);
    }
  }
  $('connection-form').addEventListener('submit',async event=>{
    event.preventDefault();
    if(!isServer) { try{ location.href=model.serverURL($('server-url').value); }catch(_){feedback('نشانی کامل HTTPS سرور خودت را وارد کن؛ کلید را داخل آدرس قرار نده.',true);} return; }
    clearTimeout(timer);clearQuote();sessionGeneration++;
    token=$('access-token').value.trim();$('access-token').value='';
    if(token.length<32){token='';feedback('کلید دسترسی معتبر این سرور لازم است.',true);return;}
    await refresh(sessionGeneration);
  });
  $('disconnect').addEventListener('click',()=>{
    token='';sessionGeneration++;clearTimeout(timer);clearQuote('بدون اتصال');
    $('connection').textContent='بدون اتصال';$('connection').classList.remove('connected');
    $('mode-detail').textContent='در انتظار اتصال سرور';
    $('disconnect').hidden=true;$('resume').hidden=true;$('pair-result').replaceChildren();
    for(const id of ['stop','pair','test-telegram'])$(id).disabled=true;
    $('levels').hidden=true;$('tf-wrap').hidden=true;$('data-label').textContent='بدون اتصال';
    $('decision').textContent='فعلاً ورود ندارد';feedback('کلید این نشست پاک شد.');
  });
  $('stop').addEventListener('click',async()=>{try{await api('/api/control/stop','POST');await refresh();}catch(e){feedback(e.message,true);}});
  $('resume').addEventListener('click',async()=>{try{await api('/api/control/resume-observation','POST');await refresh();}catch(e){feedback(e.message,true);}});
  $('pair').addEventListener('click',async()=>{try{const result=await api('/api/telegram/pair','POST');const url=new URL(result.url);if(url.protocol!=='https:'||url.hostname!=='t.me')throw new Error('نشانی تلگرام تأیید نشد.');const link=element('a','بازکردن بات و تأیید اتصال مقصد');link.href=url.href;link.target='_blank';link.rel='noreferrer noopener';$('pair-result').replaceChildren(link);}catch(e){feedback(e.message,true);}});
  $('test-telegram').addEventListener('click',async()=>{try{await api('/api/telegram/test','POST');feedback('رسید تحویل پیام غیرمعاملاتی دریافت شد.');await refresh();}catch(e){feedback(e.message,true);}});
  $('import-file').addEventListener('change',async event=>{const file=event.target.files[0];if(!file)return;try{if(file.size>2*1024*1024)throw new Error('گزارش بزرگ‌تر از ۲ مگابایت است.');renderEvaluation(JSON.parse(await file.text()),true);feedback('گزارش محلی باز شد؛ اتصال زنده ایجاد نشده است.');}catch(e){feedback(e.message||'گزارش قابل خواندن نیست.',true);}event.target.value='';});
  if(!isServer){$('server-label').hidden=false;$('token-label').hidden=true;$('connect').textContent='بازکردن سرور امن';$('connect-note').textContent='این نسخه محلی برای مشاهده گزارش است. برای اتصال، نشانی HTTPS سرور خودت را باز کن.';}
  if('serviceWorker'in navigator&&isServer)navigator.serviceWorker.register('/sw.js').catch(()=>{});
})();
