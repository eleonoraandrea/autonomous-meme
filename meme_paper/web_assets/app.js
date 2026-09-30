const money = value => new Intl.NumberFormat('it-IT', {style:'currency',currency:'USD',maximumFractionDigits:2}).format(value);
const number = value => new Intl.NumberFormat('it-IT').format(value);
const byId = id => document.getElementById(id);
const age = ms => ms == null ? '—' : ms < 60000 ? `${Math.max(0,Math.floor(ms/1000))}s` : `${Math.floor(ms/60000)}m`;
const price = value => value >= 1 ? money(value) : '$' + Number(value).toPrecision(5);
const changeText = value => value == null ? '—' : `${value>0?'+':''}${value.toFixed(Math.abs(value)<.01 && value!==0 ? 4 : 2)}%`;
let selectedMint = null;
let currentData = null;
let activeFilter = 'all';
let selectedHistory = null;
let historyKind = 'all';
let historyPage = 1;
let historyAnchor = null;
let lastHistoryAnchor = 0;
let historyRequest = 0;
let evaluations = [];
const evaluationIds = new Set();
let evaluationAnchor = 0;
let evaluationPage = 1;
let evaluationTotal = 0;
let evaluationLoading = false;
let followLive = true;
let selectedEvaluationId = null;
let stream = null;
const analysisCache = new Map();
let analysisSeq = 0;
function parameterList(id, rows) {
  const list=byId(id);list.replaceChildren();
  for(const [key,value] of rows) {
    const row=document.createElement('div'),label=document.createElement('dt'),result=document.createElement('dd');
    label.textContent=key;result.textContent=value;row.append(label,result);list.append(row);
  }
}
function inspect(market, summary) {
  const params=market?.params, features=params?.features, limits=params?.limits, rl=params?.rl;
  byId('inspect-token').textContent=market?.symbol ?? '—';
  byId('inspect-mint').textContent=market?.mint ?? 'Nessun token selezionato';
  byId('inspect-final').textContent=market?.decision ?? '—';
  byId('inspect-proposed').textContent=rl?.action ?? (rl?.mode==='not_evaluated'?'NON VALUTATA':'—');
  byId('inspect-reason').textContent=market?.reason ?? 'In attesa di dati';
  byId('training-label').textContent=summary.closed_episodes>0?'Apprendimento paper attivo':'Policy non addestrata';
  byId('training-count').textContent=`${number(summary.closed_episodes)} episodi chiusi`;
  byId('policy-summary').textContent=`${number(summary.evaluated_candidates)} candidati valutati · ${number(summary.trained_states)} stati con reward · ${number(summary.open_episodes)} episodi aperti`;
  const val=(v,format)=>v==null?'—':format(v);
  parameterList('inspect-features',[
    ['Fonte / ricevuto',features?`${params.source} · ${new Date(params.received_at).toISOString().slice(11,19)} UTC`:'—'],
    ['Prezzo USD',val(features?.price_usd,price)],
    ['Δ campione',val(features?.price_change_observed_pct,changeText)],
    ['Liquidità USD',val(features?.liquidity_usd,money)],
    ['Volume 5m',val(features?.volume_5m_usd,money)],
    ['Acquisti / vendite',features?`${features.buys_5m} / ${features.sells_5m}`:'—'],
    ['Quota buy 5m',val(features?.buy_share_5m_pct,v=>v.toFixed(1)+'%')],
    ['Δ liquidità campione',val(features?.liquidity_change_observed_pct,changeText)],
    ['Gap osservazioni',val(features?.sample_gap_s,v=>v.toFixed(0)+'s')],
    ['Età pool / dato',features?`${val(features.pair_age_s,v=>Math.round(v)+'s')} / ${val(features.data_age_s,v=>v.toFixed(1)+'s')}`:'—'],
  ]);
  const pct=v=>`${(v*100).toFixed(2)}%`;
  parameterList('inspect-policy',[
    ['Stato discreto',rl?.state ?? 'non raggiunto'],
    ['Q(BUY) / Q(SKIP)',rl?.q_buy==null?'—':`${pct(rl.q_buy)} / ${pct(rl.q_skip)}`],
    ['Esplorazione ε',val(rl?.epsilon,pct)],
    ['Campione casuale / esplora',rl?.exploration_draw==null?'—':`${rl.exploration_draw.toFixed(3)} / ${rl.exploratory?'sì':'no'}`],
    ['Reward nello stato',rl?.state_visits==null?'—':`${rl.state_visits} episodi`],
    ['Gate / size massima',limits?`${params.risk_gate} / ${limits.max_position_pct}%`:'—'],
    ['Esposizione / limite',params?.portfolio_risk?`${money(params.portfolio_risk.exposure_usd)} / ${limits.max_total_exposure_pct}% equity`:'—'],
    ['Cooldown rientro',limits?`${Math.round(limits.reentry_cooldown_s/60)} min / mint`:'—'],
    ['Margine / riserva',params?.portfolio_risk?.exposure_headroom_usd==null?'—':`${money(params.portfolio_risk.exposure_headroom_usd)} / ${money(params.portfolio_risk.cash_reserve_usd)}`],
    ['Posizioni / tetto',params?.portfolio_risk?`${params.portfolio_risk.open_positions} / ${limits.max_open_positions}`:'—'],
    ['Soglie liqu. / vol.',limits?`${money(limits.min_liquidity_usd)} / ${money(limits.min_volume_5m_usd)}`:'—'],
    ['Size / impatto stimati',params?.proposed_position_usd==null?'—':`${money(params.proposed_position_usd)} / ${val(params.estimated_entry_impact_pct,v=>v.toFixed(2)+'%')}`],
    ['Reward realizzato',val(rl?.reward_net_pct,v=>`${v>=0?'+':''}${v.toFixed(2)}%`)],
  ]);
}
function sparkline(values) {
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox','0 0 106 32'); svg.setAttribute('class','sparkline');
  svg.setAttribute('role','img'); svg.setAttribute('aria-label',`Andamento osservato su ${values.length} campioni`);
  if(values.length<2) {svg.setAttribute('aria-label','Dati insufficienti per il trend'); return svg;}
  const low=Math.min(...values),high=Math.max(...values),range=high-low;
  const points=values.map((v,i)=>`${3+i*100/(values.length-1)},${range ? 29-(v-low)*26/range : 16}`).join(' ');
  const line=document.createElementNS('http://www.w3.org/2000/svg','polyline');
  line.setAttribute('points',points); line.setAttribute('fill','none'); line.setAttribute('stroke','currentColor');
  line.setAttribute('stroke-width','2'); line.setAttribute('stroke-linejoin','round'); line.setAttribute('stroke-linecap','round');
  svg.append(line); svg.classList.add(values.at(-1)<values[0]?'negative':'positive'); return svg;
}
function cell(row, value, className) { const el = document.createElement('td'); el.textContent = value; if(className) el.className=className; row.append(el); return el; }
function emptyRow(body, text, span) { const row=document.createElement('tr'); const c=cell(row,text,'empty'); c.colSpan=span; body.replaceChildren(row); }
function renderAnalysis(market) {
  const a=market?.analysis;
  byId('analysis-token').textContent=market?.symbol ?? 'NESSUN TOKEN';
  const fmt=value=>value==null?'—':changeText(value);
  byId('analysis-return').textContent=fmt(a?.price_return_pct);
  byId('analysis-liquidity').textContent=fmt(a?.liquidity_change_pct);
  byId('analysis-drawdown').textContent=a?.sampled_max_drawdown_pct==null?'—':a.sampled_max_drawdown_pct.toFixed(2)+'%';
  byId('analysis-pressure').textContent=a?.buy_share_5m_pct==null?'—':a.buy_share_5m_pct.toFixed(1)+'%';
  byId('analysis-coverage').textContent=a?`${a.sample_count} campioni · ${a.observed_span_seconds==null?'durata ignota':age(a.observed_span_seconds*1000)+' osservati'} · gap mediano ${a.median_gap_seconds==null?'—':age(a.median_gap_seconds*1000)}`:'Nessun campione';
  const names={stale:'dato stale',sparse_samples:'meno di 3 campioni',unknown_pair_age:'età pool ignota',unsupported_quote:'quote non ammessa'};
  byId('analysis-flags').textContent=!a?'Qualità non disponibile':a.flags.length?'Limiti: '+a.flags.map(f=>names[f]??f).join(' · '):'Nessun flag nei dati disponibili (non equivale a token sicuro)';
  const series=a?.series??[], chart=byId('analysis-chart');chart.replaceChildren();
  byId('chart-span').textContent=series.length?`${a.sample_count} campioni · max 15 min`:'—';
  byId('chart-first').textContent=series.length?new Date(series[0].t).toISOString().slice(11,19)+' UTC':'—';
  byId('chart-last').textContent=series.length?new Date(series.at(-1).t).toISOString().slice(11,19)+' UTC':'—';
  if(series.length<2 || series.at(-1).t===series[0].t) {
    chart.textContent='Serve almeno una seconda osservazione ricevuta';
    chart.setAttribute('aria-label','Dati insufficienti per il grafico');return;
  }
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox','0 0 640 190');svg.setAttribute('preserveAspectRatio','none');svg.setAttribute('class','detail-chart');
  const min=Math.min(...series.map(p=>p.price)),max=Math.max(...series.map(p=>p.price));
  const x=p=>16+(p.t-series[0].t)/(series.at(-1).t-series[0].t)*608;
  const y=p=>max===min?95:170-(p.price-min)/(max-min)*150;
  for(const yy of [20,95,170]){const line=document.createElementNS('http://www.w3.org/2000/svg','line');line.setAttribute('x1','16');line.setAttribute('x2','624');line.setAttribute('y1',yy);line.setAttribute('y2',yy);line.setAttribute('class','chart-grid');svg.append(line);}
  const line=document.createElementNS('http://www.w3.org/2000/svg','polyline');
  line.setAttribute('points',series.map(p=>`${x(p)},${y(p)}`).join(' '));line.setAttribute('class','chart-line');
  if(series.at(-1).price<series[0].price)svg.classList.add('negative');
  svg.append(line);
  for(const p of [series[0],series.at(-1)]){const dot=document.createElementNS('http://www.w3.org/2000/svg','circle');dot.setAttribute('cx',x(p));dot.setAttribute('cy',y(p));dot.setAttribute('r','4');dot.setAttribute('class','chart-point');svg.append(dot);}
  chart.append(svg);chart.setAttribute('aria-label',`Prezzo osservato ${series.length} volte tra ${new Date(series[0].t).toISOString()} e ${new Date(series.at(-1).t).toISOString()}, da ${price(series[0].price)} a ${price(series.at(-1).price)}`);
}
function loadAnalysis(mint, symbol) {
  // The anatomy panel must follow the selected token, never blank out because a
  // trace came from history or the live stream instead of the on-page table.
  if(!mint){renderAnalysis(undefined);return;}
  const inPage=currentData?.markets?.find(m=>m.mint===mint);
  if(inPage){renderAnalysis(inPage);return;}
  const cached=analysisCache.get(mint);
  if(cached&&Date.now()-cached.at<20000){renderAnalysis({mint,symbol:cached.symbol,analysis:cached.analysis});return;}
  const seq=++analysisSeq;
  fetch('/api/analysis?mint='+encodeURIComponent(mint),{cache:'no-store'})
    .then(response=>{if(!response.ok)throw new Error('HTTP '+response.status);return response.json();})
    .then(data=>{
      if(seq!==analysisSeq)return;
      if(!data.found){
        renderAnalysis(undefined);
        byId('analysis-token').textContent='FUORI DAGLI ULTIMI 20';
        byId('analysis-coverage').textContent='Nessuna osservazione persistita per questo mint prima dell\'ora selezionata.';
        return;
      }
      analysisCache.set(mint,{at:Date.now(),symbol:data.symbol,analysis:data.analysis});
      renderAnalysis({mint,symbol:data.symbol??symbol,analysis:data.analysis});
    })
    .catch(()=>{
      if(seq!==analysisSeq)return;
      if(cached)renderAnalysis({mint,symbol:cached.symbol,analysis:cached.analysis});
    });
}
function renderMarketRows(data) {
  const query=byId('market-search').value.trim().toLocaleLowerCase(), sort=byId('market-sort').value;
  const list=data.markets.filter(m=>{
    if(query && !`${m.symbol} ${m.name} ${m.mint}`.toLocaleLowerCase().includes(query))return false;
    if(activeFilter==='rl')return m.params?.rl?.mode==='experimental';
    if(activeFilter==='blocked')return m.decision==='BLOCK';
    if(activeFilter==='stale')return m.analysis.flags.includes('stale');
    return true;
  });
  if(sort==='volume')list.sort((a,b)=>b.volume_5m-a.volume_5m);
  if(sort==='liquidity')list.sort((a,b)=>b.liquidity-a.liquidity);
  if(sort==='return')list.sort((a,b)=>(b.analysis.price_return_pct??-Infinity)-(a.analysis.price_return_pct??-Infinity));
  byId('market-results').textContent=`${list.length} di ${data.markets.length} token osservati in questo campione`;
  const markets=byId('markets');markets.replaceChildren();
  if(!list.length)emptyRow(markets,data.markets.length?'Nessun token corrisponde ai filtri.':'Nessun mercato osservato ancora.',8);
  for(const m of list){
    const tr=document.createElement('tr');
    const token=cell(tr,'');const box=document.createElement('div');box.className='token-stack';
    const label=document.createElement('a');label.className='token-name';label.textContent=m.symbol;
    if(/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(m.pair)){label.href='https://dexscreener.com/solana/'+m.pair;label.target='_blank';label.rel='noopener noreferrer';}
    const detail=document.createElement('span');detail.className='token-detail';detail.textContent=`${m.name} · ${m.dex} / ${m.quote} · ${age(data.now-m.received_at)} fa`;
    box.append(label,detail);token.append(box);
    cell(tr,price(m.price),'numeric price');const change=m.sample_change_pct;
    const delta=cell(tr,changeText(change),change==null?'neutral':change>=0?'up':'down');
    if(m.sample_interval_ms!=null)delta.title=`Dal precedente campione, ${age(m.sample_interval_ms)} prima`;
    const chart=cell(tr,'');chart.append(sparkline(m.history));
    cell(tr,money(m.volume_5m),'numeric');cell(tr,money(m.liquidity),'numeric');
    cell(tr,`${number(m.buys_5m)} / ${number(m.sells_5m)}`,'numeric');
    const decision=cell(tr,'');const badge=document.createElement('span');badge.className='pill '+(['BUY','SELL','BLOCK','UNFILLED'].includes(m.decision)?m.decision:'');badge.textContent=m.decision;
    const reason=document.createElement('span');reason.className='decision-detail';reason.textContent=m.reason;decision.append(badge,reason);
    const inspectButton=document.createElement('button');inspectButton.type='button';inspectButton.className='inspect-button';
    inspectButton.textContent='PARAMETRI ↗';inspectButton.setAttribute('aria-label',`Ispeziona parametri per ${m.symbol}`);
    inspectButton.addEventListener('click',()=>{selectedHistory=null;selectedMint=m.mint;inspect(m,data.rl_summary);loadAnalysis(m.mint,m.symbol);byId('policy-title').scrollIntoView({behavior:'smooth',block:'start'});});
    decision.append(inspectButton);markets.append(tr);
  }
}
const FUNNEL_LABELS={liquidity_volume:'Liquidità / volume sotto soglia',pair_age:'Età pool fuori range',unsupported_quote:'Nessuna pool wSOL/USDC',watch_momentum:'Filtro momentum/flow assente',watch_samples:'Serve un secondo campione',reentry_cooldown:'Cooldown rientro stesso mint',feed_stale:'Feed stale o futuro',invalid_market:'Mercato non valido',capital_guard:'Limiti di capitale',rl_declined:'RL → SKIP',entry:'RL → BUY eseguito',exit_sell:'Uscite SELL',exit_unfilled:'Uscite non eseguibili',hold:'Posizioni HOLD',duplicate:'Osservazioni duplicate',other_blocked:'Altri blocchi',other:'Non classificato'};
const FUNNEL_ORDER=['liquidity_volume','pair_age','unsupported_quote','watch_momentum','watch_samples','reentry_cooldown','feed_stale','invalid_market','capital_guard','rl_declined','entry','exit_sell','exit_unfilled','hold','duplicate','other_blocked','other'];
function renderFunnel(f){
  byId('funnel-version').textContent=f.strategy_version;
  byId('funnel-total').textContent=number(f.observations)+' OSSERVAZIONI';
  const max=Math.max(1,...FUNNEL_ORDER.map(k=>f.buckets[k]??0));
  const steps=byId('funnel-steps');steps.replaceChildren();
  for(const key of FUNNEL_ORDER){
    const count=f.buckets[key]??0;if(!count)continue;
    const row=document.createElement('div');row.className='funnel-step'+(['rl_declined','entry'].includes(key)?' funnel-rl':'');
    const label=document.createElement('span');label.textContent=FUNNEL_LABELS[key]??key;
    const track=document.createElement('i');const fill=document.createElement('b');
    fill.style.width=Math.max(2,Math.round(count/max*100))+'%';track.append(fill);
    const value=document.createElement('strong');value.textContent=number(count);
    const pct=document.createElement('small');pct.textContent=f.observations?((count/f.observations)*100).toFixed(1)+'%':'0%';
    row.append(label,track,value,pct);steps.append(row);
  }
  if(!steps.childElementCount){const empty=document.createElement('span');empty.className='empty-line';empty.textContent='Nessuna decisione con questa versione della strategia nelle ultime 24 ore.';steps.append(empty);}
  const list=byId('funnel-reasons');list.replaceChildren();
  if(!f.top_reasons.length){const li=document.createElement('li');li.textContent='—';list.append(li);}
  for(const r of f.top_reasons.slice(0,8)){const li=document.createElement('li');const c=document.createElement('b');c.textContent=number(r.count);const t=document.createElement('span');t.textContent=r.reason;li.append(c,t);list.append(li);}
}
function render(data) {
  currentData=data;
  byId('clock').textContent = new Date().toISOString().slice(11,19) + ' UTC';
  if (data.missing) {
    byId('alert').textContent = 'Database non ancora disponibile. Avvia il collector paper; la pagina si aggiorna automaticamente.';
    byId('alert').className = 'alert warning';
    return;
  }
  const discovery=data.discovery;
  const lastDiscovery=discovery.last_event_at==null?null:Math.max(0,Date.now()-discovery.last_event_at);
  const feedLive=discovery.status==='connected' && lastDiscovery!=null && lastDiscovery<120000;
  byId('discovery-status').textContent=feedLive?'● Collegato':discovery.status==='connected'?'○ In attesa di eventi':discovery.status==='reconnecting'?'○ Riconnessione':'○ '+discovery.status;
  byId('discovery-status').className=feedLive?'up':'neutral';
  byId('discovery-events').textContent=number(discovery.events);
  byId('discovery-priced').textContent=`${number(discovery.priced_mints)} / ${number(discovery.unique_mints)}`;
  byId('discovery-note').textContent=discovery.last_error&&discovery.status==='reconnecting'?'Connessione interrotta; nuovo tentativo automatico. Nessuna nuova valutazione dai soli eventi.':
    `Ultimo evento ${lastDiscovery==null?'mai':age(lastDiscovery)+' fa'} · prezzi disponibili solo per i mint con snapshot DexScreener.`;
  const creations=byId('discovery-list');creations.replaceChildren();
  if(!discovery.recent.length){const li=document.createElement('li');li.textContent='Nessuna creazione registrata';creations.append(li);}
  for(const item of discovery.recent){const li=document.createElement('li');
    const time=document.createElement('time');time.dateTime=new Date(item.received_at).toISOString();time.textContent=time.dateTime.slice(11,19);
    const token=document.createElement('span');token.textContent=item.symbol;token.title=item.mint;
    const state=document.createElement('b');state.textContent=item.first_priced==null?'Nessuna pool prezzata':'Prezzo rilevato';state.className=item.first_priced==null?'neutral':'up';
    li.append(time,token,state);creations.append(li);}
  const lag = data.latest == null ? null : Math.max(0, Date.now() - data.latest);
  byId('cash').textContent=money(data.cash);
  byId('equity').textContent=money(data.estimated);
  byId('positions-count').textContent=number(data.positions.length);
  const risk=data.capital_risk;
  byId('capital-state').textContent=risk.entry_paused?'● Nuove entrate sospese':'● Nuove entrate consentite dai limiti di portafoglio';
  byId('capital-state').className=risk.entry_paused?'down':'up';
  byId('capital-exposure').textContent=`${money(risk.exposure_usd)} / ${money(risk.exposure_cap_usd)}`;
  byId('capital-headroom').textContent=money(risk.headroom_usd);
  byId('capital-reserve').textContent=`${money(data.cash)} / ${money(risk.cash_reserve_usd)}`;
  byId('capital-positions').textContent=`${number(data.positions.length)} / ${risk.max_positions}`;
  renderFunnel(data.funnel);
  byId('capital-reasons').textContent=risk.reasons.length?'Motivi: '+risk.reasons.join(' · '):
    'I limiti di portafoglio consentono nuove entrate se anche mercato e strategia superano i propri controlli. Gli stop non garantiscono l’uscita.';
  byId('observations').textContent=number(data.observations);
  byId('fills').textContent=number(data.fills);
  const badge=byId('open-trades-count');badge.textContent=number(data.fills);badge.hidden=data.fills===0;
  byId('feed-age').textContent=age(lag);
  byId('feed-note').textContent=lag == null ? 'Nessuna osservazione' : 'Dall’ultima osservazione';
  byId('market-count').textContent=number(data.markets.length)+' MERCATI';
  byId('header-rl-total').textContent=number(data.rl_summary.evaluated_candidates);
  byId('header-rl-buy').textContent=number(data.decision_summary.rl_actions.BUY??0);
  byId('header-rl-skip').textContent=number(data.decision_summary.rl_actions.SKIP??0);
  byId('header-decisions').textContent=number(data.decision_summary.total);
  for(const [id,value] of Object.entries({'cohort-observed':data.cohort.observed,'cohort-fresh':data.cohort.fresh,'cohort-supported':data.cohort.supported_quote,'cohort-blocked':data.cohort.blocked}))byId(id).textContent=number(value);
  byId('cohort-total').textContent=`${number(data.cohort.historical_unique)} token distinti nel database`; 
  const state = lag == null ? 'Nessun dato ricevuto' : lag > 120000 ? 'Feed inattivo: ultima osservazione oltre 2 minuti fa' : 'DexScreener · ultima osservazione '+age(lag)+' fa · aggiornamento schermata ogni 5s';
  byId('alert').textContent=state;
  byId('alert').className=lag == null || lag > 120000 ? 'alert warning' : 'alert';
  byId('updated').textContent='Ultimo aggiornamento ' + new Date().toLocaleTimeString('it-IT');
  renderMarketRows(data);
  const lastRL=data.rl_summary.last_policy_decision;
  const lastTrace=lastRL && {mint:lastRL.mint,symbol:data.markets.find(m=>m.mint===lastRL.mint)?.symbol ?? lastRL.mint.slice(0,12),
                            decision:lastRL.action,reason:lastRL.reason,params:lastRL.params};
  const focusMint=selectedHistory?.mint ?? selectedMint ?? lastTrace?.mint ?? data.markets[0]?.mint ?? null;
  inspect(selectedHistory ?? data.markets.find(m=>m.mint===selectedMint) ?? lastTrace ?? data.markets[0],data.rl_summary);
  loadAnalysis(focusMint,selectedHistory?.symbol ?? lastTrace?.symbol ?? data.markets[0]?.symbol);
  const positions=byId('positions'); positions.replaceChildren();
  if(!data.positions.length) emptyRow(positions,'Nessuna posizione aperta.',4);
  for (const p of data.positions) {
    const tr=document.createElement('tr'); cell(tr,p.mint.slice(0,12)+'…','mint'); cell(tr,money(p.cost));
    cell(tr,money(p.units*(p.price ?? p.entry_price)*.96)); cell(tr,age(p.seen == null ? null : data.now-p.seen)); positions.append(tr);
  }
}
function renderHistory(data) {
  lastHistoryAnchor=data.anchor;
  const totalPages=Math.max(1,Math.ceil(data.total/data.page_size));
  byId('history-count').textContent=`${number(data.total)} ${data.kind==='rl'?'valutazioni RL':'decisioni'} · ${historyAnchor==null?'live':'istantanea congelata'}`;
  byId('history-page').textContent=`Pagina ${data.page} / ${totalPages}`;
  byId('history-prev').disabled=data.page<=1;
  byId('history-next').disabled=data.page>=totalPages;
  byId('history-live').hidden=historyAnchor==null;
  const body=byId('decisions');body.replaceChildren();
  if(!data.items.length)emptyRow(body,'Nessuna decisione in questa vista.',7);
  for(const d of data.items){
    const row=document.createElement('tr');
    cell(row,new Date(d.received_at).toISOString().replace('T',' ').slice(0,19),'history-time');
    const token=cell(row,'');const symbol=document.createElement('strong');symbol.textContent=d.symbol??d.mint.slice(0,12);
    const mint=document.createElement('small');mint.textContent=d.mint;mint.title=d.mint;token.append(symbol,mint);
    const action=cell(row,'');const badge=document.createElement('span');badge.className='pill '+(['BUY','SELL','BLOCK','UNFILLED'].includes(d.action)?d.action:'');badge.textContent=d.action;action.append(badge);
    const rl=d.params?.rl;cell(row,rl?.state??'—');
    cell(row,rl?.q_buy==null?'—':(rl.q_buy*100).toFixed(2)+'%');
    cell(row,rl?.epsilon==null?'—':(rl.epsilon*100).toFixed(1)+'%');
    const details=cell(row,'');const reason=document.createElement('span');reason.className='history-reason';reason.textContent=d.reason;
    const button=document.createElement('button');button.type='button';button.className='inspect-button';button.textContent='ISPEZIONA ↗';
    button.setAttribute('aria-label',`Ispeziona la decisione ${d.id} per ${d.symbol??d.mint}`);
    button.addEventListener('click',()=>{selectedMint=null;selectedHistory={mint:d.mint,symbol:d.symbol??d.mint.slice(0,12),decision:d.action,reason:d.reason,params:d.params};
      inspect(selectedHistory,currentData.rl_summary);loadAnalysis(d.mint,d.symbol);byId('policy-title').scrollIntoView({behavior:'smooth',block:'start'});});
    details.append(reason,button);body.append(row);
  }
}
function selectEvaluation(item, automatic=false) {
  if(!automatic)followLive=false;
  selectedEvaluationId=item.id;
  selectedMint=null;
  selectedHistory={mint:item.mint,symbol:item.symbol??item.mint.slice(0,12),
                   decision:item.action,reason:item.reason,params:item.params};
  if(currentData&&!currentData.missing){inspect(selectedHistory,currentData.rl_summary);loadAnalysis(item.mint,item.symbol);}
  byId('rl-follow-live').hidden=followLive;
  renderTimeline();
}
function renderTimeline() {
  byId('rl-stream-count').textContent=`${number(evaluations.length)} di ${number(evaluationTotal)} valutazioni caricate`;
  byId('rl-load-older').hidden=evaluations.length>=evaluationTotal;
  const list=byId('rl-timeline');list.replaceChildren();
  if(!evaluations.length){const li=document.createElement('li');li.className='timeline-empty';li.textContent='Nessuna valutazione RL registrata ancora. I filtri deterministici restano attivi.';list.append(li);}
  for(const item of evaluations){
    const li=document.createElement('li'),button=document.createElement('button');
    button.type='button';button.className='evaluation-card'+(selectedEvaluationId===item.id?' selected':'');
    button.setAttribute('aria-pressed',String(selectedEvaluationId===item.id));
    button.setAttribute('aria-label',`Valutazione RL ${item.id}: ${item.symbol??item.mint}, ${item.action}, ${new Date(item.received_at).toISOString()}`);
    const top=document.createElement('span');top.className='evaluation-top';
    const time=document.createElement('time');time.dateTime=new Date(item.received_at).toISOString();time.textContent=time.dateTime.slice(11,19)+' UTC';
    const action=document.createElement('b');action.textContent=item.action;action.className=item.action==='BUY'?'up':'neutral';top.append(time,action);
    const token=document.createElement('strong');token.textContent=item.symbol??item.mint.slice(0,12);
    const detail=document.createElement('small');const rl=item.params?.rl;
    detail.textContent=`${rl?.state??'stato ignoto'} · Q(BUY) ${rl?.q_buy==null?'—':(rl.q_buy*100).toFixed(2)+'%'} · ε ${rl?.epsilon==null?'—':(rl.epsilon*100).toFixed(1)+'%'}`;
    button.append(top,token,detail);button.addEventListener('click',()=>selectEvaluation(item));li.append(button);list.append(li);
  }
}
async function bootstrapTimeline() {
  if(evaluationLoading||stream)return;
  evaluationLoading=true;
  try {
    const response=await fetch('/api/decisions?kind=rl&page=1',{cache:'no-store'});
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    const page=await response.json();evaluationAnchor=page.anchor;evaluationTotal=page.total;evaluationPage=1;
    evaluations=page.items;for(const item of evaluations)evaluationIds.add(item.id);
    renderTimeline();if(followLive&&evaluations.length)selectEvaluation(evaluations[0],true);
    stream=new EventSource('/api/events?since='+evaluationAnchor);
    stream.addEventListener('open',()=>{byId('rl-stream-state').textContent='● STREAM / LIVE';byId('rl-event-status').textContent='In ascolto di nuove valutazioni';});
    stream.addEventListener('error',()=>{byId('rl-stream-state').textContent='○ STREAM / RECONNECTING';byId('rl-event-status').textContent='Connessione interrotta; riconnessione automatica';});
    stream.addEventListener('rl-evaluation',event=>{
      const item=JSON.parse(event.data);
      if(evaluationIds.has(item.id))return;
      evaluationIds.add(item.id);evaluations.unshift(item);evaluationTotal++;
      byId('rl-event-status').textContent=`Nuova valutazione: ${item.symbol??item.mint.slice(0,12)} → ${item.action}`;
      if(followLive)selectEvaluation(item,true);else renderTimeline();
      refreshStatus();if(historyPage===1)refreshHistory();
    });
  } catch(error) {
    byId('rl-stream-state').textContent='○ STREAM / WAITING';
    byId('rl-event-status').textContent='Storico non disponibile; riprovo automaticamente';
  } finally {evaluationLoading=false;}
}
async function loadOlderEvaluations() {
  if(evaluationLoading||evaluations.length>=evaluationTotal)return;
  evaluationLoading=true;byId('rl-load-older').disabled=true;
  try {
    const next=evaluationPage+1;
    const query=new URLSearchParams({kind:'rl',page:String(next),anchor:String(evaluationAnchor)});
    const response=await fetch('/api/decisions?'+query,{cache:'no-store'});
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    const data=await response.json();evaluationPage=next;
    for(const item of data.items){if(!evaluationIds.has(item.id)){evaluationIds.add(item.id);evaluations.push(item);}}
    renderTimeline();
  } catch(error){byId('rl-event-status').textContent='Impossibile caricare le valutazioni precedenti: '+error.message;}
  finally {evaluationLoading=false;byId('rl-load-older').disabled=false;}
}
async function refreshHistory() {
  const seq=++historyRequest;
  const query=new URLSearchParams({kind:historyKind,page:String(historyPage)});
  if(historyAnchor!=null)query.set('anchor',String(historyAnchor));
  try {
    const response=await fetch('/api/decisions?'+query,{cache:'no-store'});
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    const data=await response.json();
    if(seq===historyRequest)renderHistory(data);
  } catch(error) {
    if(seq===historyRequest)byId('history-count').textContent='Registro temporaneamente non disponibile: '+error.message;
  }
}
function setHistoryKind(kind) {
  historyKind=kind;historyPage=1;historyAnchor=null;
  document.querySelectorAll('.history-chip').forEach(button=>{const active=button.dataset.historyKind===kind;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});
  refreshHistory();
}
const tradesDialog=byId('trades-dialog');
let opsPage=1,opsAnchor=null,lastOpsAnchor=0,opsRequest=0;
function opsChip(label,value,cls){const s=document.createElement('span');s.textContent=label+' ';const b=document.createElement('strong');b.textContent=value;if(cls)b.className=cls;s.append(b);return s;}
function renderOperations(data){
  lastOpsAnchor=data.anchor;
  const totalPages=Math.max(1,Math.ceil(data.total/data.page_size));
  const s=data.summary;
  byId('ops-status').textContent=`${number(data.total)} operazioni · ${opsAnchor==null?'live':'istantanea congelata'} · aggiornamento ogni 5s`;
  byId('ops-summary').replaceChildren(
    opsChip('OPERAZIONI',number(s.buys+s.sells)),
    opsChip('BUY',number(s.buys)),
    opsChip('SELL',number(s.sells)),
    opsChip('P&L NETO EPISODI CHIUSI',(s.closed_pnl_usd>=0?'+':'')+money(s.closed_pnl_usd),s.closed_pnl_usd>=0?'up':'down'),
    opsChip('FEE TOTALI',money(s.fees_usd),'neutral'),
    opsChip('POSIZIONI APERTE',number(s.open_positions)),
    opsChip('USCITE NON ESEGUIBILI',number(s.unfilled_exits)));
  byId('ops-page').textContent=`Pagina ${data.page} / ${totalPages}`;
  byId('ops-prev').disabled=data.page<=1;
  byId('ops-next').disabled=data.page>=totalPages;
  byId('ops-live').hidden=opsAnchor==null;
  const body=byId('ops-rows');body.replaceChildren();
  if(!data.items.length){emptyRow(body,'Nessuna operazione paper registrata.',8);return;}
  for(const f of data.items){
    const tr=document.createElement('tr');
    cell(tr,new Date(f.received_at).toISOString().replace('T',' ').slice(0,19),'history-time');
    const token=cell(tr,'');const strong=document.createElement('strong');strong.textContent=f.symbol;
    const mint=document.createElement('small');mint.textContent=f.mint;mint.title=f.mint;token.append(strong,mint);
    const side=cell(tr,'');const badge=document.createElement('span');badge.className='pill '+f.side;badge.textContent=f.side;side.append(badge);
    cell(tr,Number(f.units).toLocaleString('it-IT',{maximumFractionDigits:6}),'numeric');
    cell(tr,price(f.price),'numeric');
    cell(tr,(f.cash_delta>=0?'+':'')+money(f.cash_delta),'numeric '+(f.cash_delta>=0?'up':'down'));
    cell(tr,money(f.fee),'numeric neutral');
    const ep=cell(tr,'');const info=document.createElement('span');info.className='history-reason';
    if(f.side==='BUY'){info.textContent=f.episode_state?`stato ${f.episode_state} · ${f.episode_status}`:'posizione non-RL';info.className+=' neutral';}
    else if(f.episode_reward==null){info.textContent='chiusura senza episodio RL';info.className+=' neutral';}
    else{info.textContent=`reward netto ${(f.episode_reward*100).toFixed(2)}%`;info.className+=' '+(f.episode_reward>=0?'up':'down');}
    ep.append(info);body.append(tr);
  }
}
async function refreshOperations(){
  const seq=++opsRequest;
  const query=new URLSearchParams({page:String(opsPage)});
  if(opsAnchor!=null)query.set('anchor',String(opsAnchor));
  try{
    const response=await fetch('/api/operations?'+query,{cache:'no-store'});
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    const data=await response.json();
    if(seq===opsRequest)renderOperations(data);
  }catch(error){
    if(seq===opsRequest)byId('ops-status').textContent='Storico temporaneamente non disponibile: '+error.message;
  }
}
byId('market-search').addEventListener('input',()=>{if(currentData&&!currentData.missing)renderMarketRows(currentData);});
byId('market-sort').addEventListener('change',()=>{if(currentData&&!currentData.missing)renderMarketRows(currentData);});
document.querySelectorAll('.filter-chip').forEach(button=>button.addEventListener('click',()=>{
  activeFilter=button.dataset.filter;
  document.querySelectorAll('.filter-chip').forEach(chip=>{const selected=chip===button;chip.classList.toggle('active',selected);chip.setAttribute('aria-pressed',String(selected));});
  if(currentData&&!currentData.missing)renderMarketRows(currentData);
}));
document.querySelectorAll('.history-chip').forEach(button=>button.addEventListener('click',()=>setHistoryKind(button.dataset.historyKind)));
document.querySelectorAll('[data-history-filter]').forEach(link=>link.addEventListener('click',()=>setHistoryKind(link.dataset.historyFilter)));
byId('history-prev').addEventListener('click',()=>{if(historyPage>1){historyPage--;if(historyPage===1)historyAnchor=null;refreshHistory();}});
byId('history-next').addEventListener('click',()=>{historyAnchor??=lastHistoryAnchor;historyPage++;refreshHistory();});
byId('history-live').addEventListener('click',()=>{historyPage=1;historyAnchor=null;refreshHistory();});
byId('rl-load-older').addEventListener('click',loadOlderEvaluations);
byId('rl-follow-live').addEventListener('click',()=>{followLive=true;if(evaluations.length)selectEvaluation(evaluations[0],true);byId('rl-follow-live').hidden=true;});
function openOperations(){opsPage=1;opsAnchor=null;tradesDialog.showModal();refreshOperations();}
byId('open-trades').addEventListener('click',openOperations);
byId('trades-close').addEventListener('click',()=>tradesDialog.close());
tradesDialog.addEventListener('click',event=>{if(event.target===tradesDialog)tradesDialog.close();}); // click sul backdrop
byId('ops-prev').addEventListener('click',()=>{if(opsPage>1){opsPage--;if(opsPage===1)opsAnchor=null;refreshOperations();}});
byId('ops-next').addEventListener('click',()=>{opsAnchor??=lastOpsAnchor;opsPage++;refreshOperations();});
byId('ops-live').addEventListener('click',()=>{opsPage=1;opsAnchor=null;refreshOperations();});
window.addEventListener('beforeunload',()=>stream?.close());
async function refreshStatus() {
  try {
    const response=await fetch('/api/status',{cache:'no-store'});
    if(!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch(error) {
    byId('alert').textContent='Dashboard non disponibile; nuovo tentativo tra 5s ('+error.message+')';
    byId('alert').className='alert error';
  }
}
function refresh(){refreshStatus();refreshHistory();if(!stream)bootstrapTimeline();}
refresh();setInterval(refresh,5000);
