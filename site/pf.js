/* 내 자산 — 매매·입출금·기타 자산 기록은 이 브라우저(localStorage)에만 저장됩니다.
   서버에서는 공개 정보(시세 prices.json, 지표 market.json, 해자 moat.json)만 받아서 폰에서 계산합니다. */
'use strict';
let PX=null, MK=null, MO=null, pfSeg=store.get('pfSeg','asset'), pfLoading=null;
let pfAlloc=store.get('pfAlloc','stock'), pfPer=store.get('pfPer','90'), pfGrain=store.get('pfGrain','m');
let recKind='B', pfOpen={};
const PFKEY='pf';
const KRW=v=>v==null||!isFinite(v)?'-':(v<0?'-':'')+Math.round(Math.abs(v)).toLocaleString('ko-KR')+'원';
const KRWs=v=>{if(v==null||!isFinite(v))return '-';const a=Math.abs(v),s=v<0?'-':'';
  if(a>=1e8)return s+(a/1e8).toFixed(a>=1e10?0:2).replace(/\.?0+$/,'')+'억';if(a>=1e4)return s+Math.round(a/1e4).toLocaleString('ko-KR')+'만';return s+Math.round(a).toLocaleString('ko-KR');};
const PCT=(v,d=2)=>v==null||!isFinite(v)?'-':(v>0?'+':'')+(v*100).toFixed(d)+'%';
const sgnCls=v=>v>0?'up':(v<0?'dn':'');
const today=()=>kstParts(new Date()).date;
const d2i=d=>+d.replace(/-/g,'');
const i2d=i=>{const s=String(i);return `${s.slice(0,4)}-${s.slice(4,6)}-${s.slice(6,8)}`;};
const uid=()=>Date.now().toString(36)+Math.random().toString(36).slice(2,6);

/* ---------- 저장 ---------- */
function pfGet(){const p=store.get(PFKEY,null)||{};return {v:1,auto:p.auto!==false,trades:p.trades||[],flows:p.flows||[],others:p.others||[]};}
function pfSet(p){store.set(PFKEY,p);}

/* ---------- 공개 데이터 받기 ---------- */
function pfLoad(){
  if(pfLoading) return pfLoading;
  const get=n=>fetch(`${n}?t=${Date.now()}`,{cache:'no-store'}).then(r=>r.ok?r.json():null).catch(()=>null);
  pfLoading=Promise.all([get('data/prices.json'),get('data/market.json'),get('moat.json')]).then(([a,b,c])=>{PX=a;MK=b;MO=c;});
  return pfLoading;
}

/* ---------- 시세 조회 ---------- */
function series(k){return PX&&PX.px&&PX.px[k];}
function lastLE(ser,di){                 // di 이하 마지막 값
  if(!ser||!ser.t||!ser.t.length) return null;
  const t=ser.t; if(di<t[0]) return null;
  let lo=0,hi=t.length-1;
  while(lo<hi){const m=(lo+hi+1)>>1; if(t[m]<=di) lo=m; else hi=m-1;}
  return ser.c[lo];
}
const fxAt=di=>lastLE(PX&&PX.fx,di)||lastLE(PX&&PX.fx,99999999)||1400;
const isUS=k=>k.startsWith('US:');
function nameOf(k){
  const s=(D?.stocks||[]).find(x=>x.id===k); if(s) return s.name;
  const m=MK?.info?.[k]; if(m?.name) return m.name;
  const t=pfGet().trades.find(x=>x.k===k); return t?.name||k.split(':')[1];
}

/* ---------- 계산 ----------
   날마다(평일) 보유 수량 × 그날까지의 마지막 종가로 평가.
   예수금 = 입금 − 출금 + 배당·이자 − 매수 + 매도 (원화, 미국 종목은 매매일 환율).
   auto=true 이면 예수금이 모자랄 때 모자란 만큼 입금한 것으로 봄(원금에 포함).
   일 수익률 = (오늘 평가액 − 오늘 들어온 원금) / 어제 평가액 − 1  → 월·연은 일 수익률을 이어 곱함(시간가중). */
function pfCalc(p){
  const ev=[];
  p.trades.forEach(t=>ev.push({d:t.d,o:t.side==='B'?2:3,t}));
  p.flows.forEach(f=>ev.push({d:f.d,o:f.kind==='in'?0:(f.kind==='out'?4:1),f}));
  if(!ev.length && !p.others.length) return null;
  ev.sort((a,b)=>a.d<b.d?-1:a.d>b.d?1:a.o-b.o);
  const starts=[...ev.map(e=>e.d),...p.others.flatMap(o=>o.snaps.map(s=>s.d))].sort();
  const start=starts[0], end=today();
  const days=[]; {const d=new Date(start+'T12:00:00+09:00'), e=new Date(end+'T12:00:00+09:00');
    for(;d<=e;d.setDate(d.getDate()+1)){const w=d.getDay(); if(w&&w<6) days.push(kstParts(d).date);} if(!days.length||days[days.length-1]!==end) days.push(end);}
  const qty={}, cost={}, costN={}, realized={}, warn=[];
  let cash=0, prin=0, autoIn=0, ei=0, inc=0, fees=0;
  const out=[]; let prevV=null;
  const oth=p.others.map(o=>({o,s:[...o.snaps].sort((a,b)=>a.d<b.d?-1:1)}));
  for(const day of days){
    const di=d2i(day);
    let cf=0;
    while(ei<ev.length && ev[ei].d<=day){
      const e=ev[ei++];
      if(e.f){const a=+e.f.amt||0;
        if(e.f.kind==='in'){cash+=a;prin+=a;cf+=a;} else if(e.f.kind==='out'){cash-=a;prin-=a;cf-=a;} else {cash+=a;inc+=a;}
        continue;}
      const t=e.t, us=isUS(t.k), fx=us?(+t.fx||fxAt(d2i(t.d))):1, q=+t.q, px=+t.p, fee=+t.fee||0, amt=q*px*fx;
      fees+=fee;
      if(t.side==='B'){
        cash-=amt+fee;
        qty[t.k]=(qty[t.k]||0)+q; cost[t.k]=(cost[t.k]||0)+amt+fee; costN[t.k]=(costN[t.k]||0)+q*px;
      }else{
        const have=qty[t.k]||0;
        if(q>have+1e-9) warn.push(`${t.d} ${nameOf(t.k)}: 가진 수량(${have})보다 많이 팔았습니다`);
        const ratio=have>0?Math.min(1,q/have):0, c=(cost[t.k]||0)*ratio;
        realized[t.k]=(realized[t.k]||0)+amt-fee-c;
        cost[t.k]=(cost[t.k]||0)-c; costN[t.k]=(costN[t.k]||0)*(1-ratio); qty[t.k]=have-q;
        cash+=amt-fee;
      }
      if(cash<-0.5){
        if(p.auto){autoIn+=-cash;prin+=-cash;cf+=-cash;cash=0;}
      }
    }
    let sv=0;
    for(const k in qty){ if(Math.abs(qty[k])<1e-9) continue;
      const c=lastLE(series(k),di); if(c==null) continue;
      sv+=qty[k]*c*(isUS(k)?fxAt(di):1); }
    let ov=0, op=0;
    oth.forEach(x=>{let v=null,pp=null; for(const s of x.s){ if(s.d<=day){v=+s.val;pp=+s.prin;} else break;} if(v!=null){ov+=v;op+=pp||0;}});
    const P=prin+op, V=sv+cash+ov;
    const prevP=out.length?out[out.length-1].P:0, flow=P-prevP;
    let r=null; if(prevV!=null && prevV>0) r=(V-flow)/prevV-1;
    out.push({d:day,V,P,S:sv,C:cash,O:ov,r,pl:prevV!=null?V-prevV-flow:0});
    prevV=V;
  }
  // 현재 보유 내역
  const di=d2i(end), hold=[];
  for(const k in qty){ if(qty[k]<=1e-9) continue;
    const c=lastLE(series(k),di), fx=isUS(k)?fxAt(di):1;
    hold.push({k,q:qty[k],cost:cost[k],avg:costN[k]/qty[k],price:c,val:c!=null?qty[k]*c*fx:null,real:realized[k]||0});
  }
  if(cash<-0.5) warn.push(`예수금이 ${KRW(cash)}입니다. 입금 기록을 넣거나 '모자란 매수금은 입금으로 보기'를 켜 주세요`);
  const nowRow=out[out.length-1];
  return {rows:out,hold,cash,others:oth.map(x=>({...x.o,last:x.s.filter(s=>s.d<=end).pop()})).filter(o=>o.last),autoIn,inc,fees,realized,warn,now:nowRow};
}

/* ---------- 공통 ---------- */
function pfSectorOf(k){
  const m=MO?.items?.[k]; if(m?.sector) return m.sector;
  const i=MK?.info?.[k]; if(/ETF|ETN/i.test(nameOf(k))) return 'ETF';
  return i?.ind?i.ind.replace(/ 제조업$| 서비스업$|업$/,''):'기타';
}
function moatChip(k){const m=MO?.items?.[k];if(!m)return '';const c=m.moat==='넓음'?'buy':(m.moat==='좁음'?'trust':'neu');return `<span class="k ${c}">해자 ${esc(m.moat)}</span>`;}
function fv(v,suf,d=1){return v==null||!isFinite(v)?'-':(+v).toFixed(d)+suf;}

/* ---------- 자산 ---------- */
function drawAsset(el,R,p){
  if(!R||!R.now){el.innerHTML=`<div class="empty">아직 기록이 없습니다.<br><b>기록</b>에서 매수 내역과 입금(원금)을 넣으면 비중·수익률이 계산됩니다.</div>${pfMissing([])}`;bindMissing(el);return;}
  const N=R.now, prev=R.rows.length>1?R.rows[R.rows.length-2]:null, pl=N.V-N.P, ret=N.P>0?pl/N.P:null;
  const dayPl=prev?N.pl:0, dayR=N.r;
  let h=`<div class="hero2"><div class="lbl">총 평가액</div><div class="big2">${KRW(N.V)}</div>
    <div class="l2 ${sgnCls(pl)}">원금 대비 <b>${pl>=0?'+':''}${KRW(pl)}</b> (${PCT(ret)})</div>
    <div class="sum3"><div>원금<b>${KRWs(N.P)}</b></div><div>오늘<b class="${sgnCls(dayPl)}">${dayPl>=0?'+':''}${KRWs(dayPl)}</b><small class="${sgnCls(dayR)}">${PCT(dayR)}</small></div><div>주식·ETF<b>${KRWs(N.S)}</b></div></div></div>`;
  if(R.warn.length) h+=`<div class="warn">${R.warn.slice(0,3).map(esc).join('<br>')}</div>`;
  // 비중
  const items=R.hold.filter(x=>x.val!=null).map(x=>({k:x.k,name:nameOf(x.k),val:x.val,sec:pfSectorOf(x.k)}));
  if(N.C>0.5) items.push({k:'cash',name:'예수금',val:N.C,sec:'현금성'});
  R.others.forEach(o=>items.push({k:'o:'+o.id,name:o.name,val:+o.last.val,sec:o.type==='펀드'||o.type==='연금'?o.type:'현금성'}));
  const tot=items.reduce((a,b)=>a+b.val,0)||1;
  let rows;
  if(pfAlloc==='sector'){const g={};items.forEach(i=>{g[i.sec]=(g[i.sec]||0)+i.val;});rows=Object.entries(g).map(([n,v])=>({name:n,val:v}));}
  else rows=items.map(i=>({name:i.name,val:i.val,sub:i.sec}));
  rows.sort((a,b)=>b.val-a.val);
  const mx=rows.length?rows[0].val/tot:1;
  h+=`<h2 class="sec-title">비중</h2><div class="seg" role="group" aria-label="비중 기준" id="pfAllocSeg"><button data-v="stock" aria-pressed="${pfAlloc==='stock'}">종목별</button><button data-v="sector" aria-pressed="${pfAlloc==='sector'}">산업군별</button></div>
    <div class="list" style="margin-top:8px">${rows.map(r=>{const w=r.val/tot;return `<div class="al"><div class="al-n">${esc(r.name)}${r.sub?`<small>${esc(r.sub)}</small>`:''}</div><div class="al-b"><i style="width:${(w/mx*100).toFixed(1)}%"></i></div><div class="al-v"><b>${(w*100).toFixed(1)}%</b><small>${KRWs(r.val)}</small></div></div>`;}).join('')}</div>`;
  // 보유 종목
  const H=[...R.hold].sort((a,b)=>(b.val||0)-(a.val||0));
  h+=`<h2 class="sec-title">보유 종목 <span class="mute" style="font-size:14px">${H.length}개</span></h2><div class="list">`;
  h+=H.map(x=>{
    const us=isUS(x.k), info=MK?.info?.[x.k]||{}, m=MO?.items?.[x.k], gain=x.val!=null?x.val-x.cost:null, gr=x.cost>0&&gain!=null?gain/x.cost:null;
    const pxs=x.price==null?'-':(us?'$'+x.price.toFixed(2):Math.round(x.price).toLocaleString('ko-KR')+'원');
    const avg=us?'$'+x.avg.toFixed(2):Math.round(x.avg).toLocaleString('ko-KR')+'원';
    const open=pfOpen[x.k];
    return `<div class="hd"><button class="hd-b" data-hk="${esc(x.k)}" aria-expanded="${!!open}">
      <div class="h"><span class="co">${esc(nameOf(x.k))}</span><span class="mk">${us?'미국':'국내'}</span><span class="sec">${esc(pfSectorOf(x.k))}</span><span class="sp"></span>${moatChip(x.k)}</div>
      <div class="hd-g"><div>평가액<b>${KRWs(x.val)}</b></div><div>비중<b>${x.val!=null?(x.val/tot*100).toFixed(1)+'%':'-'}</b></div><div>수익률<b class="${sgnCls(gr)}">${PCT(gr,1)}</b></div></div>
      <div class="l3">${(+x.q).toLocaleString('ko-KR')}주 · 평단 ${avg} · 현재 ${pxs}${gain!=null?` · <span class="${sgnCls(gain)}">${gain>=0?'+':''}${KRWs(gain)}</span>`:''}</div>
      <div class="fm"><span>PER <b>${fv(info.per,'',1)}</b></span><span>PBR <b>${fv(info.pbr,'',2)}</b></span><span>배당 <b>${fv(info.div,'%',2)}</b></span><span>ROE <b>${fv(info.roe,'%',1)}</b>${info.roe_est?'<small>추정</small>':''}</span></div></button>
      ${open?`<div class="hd-x">${m?`<div class="mo"><b>해자 ${esc(m.moat)}</b> · 추세 ${esc(m.trend||'-')}${m.sources?.length?` · ${m.sources.map(esc).join('·')}`:''}</div><p>${esc(m.why)}</p><p class="mute"><b>리스크</b> ${esc(m.risk)}</p>`:'<p class="mute">이 종목은 아직 해자 분석이 없습니다. Claude에게 분석을 요청해 주세요.</p>'}
        <p class="mute" style="font-size:12px">${info.ind?`업종: ${esc(info.ind)} · `:''}${info.mcap?`시총 ${KRWs(us?info.mcap*fxAt(d2i(today())):info.mcap)}원 · `:''}${info.fper?`예상 PER ${fv(info.fper,'',1)} · `:''}${x.real?`실현손익 ${KRWs(x.real)}원`:''}</p></div>`:''}</div>`;
  }).join('')||'<div class="empty" style="border-radius:0">보유 중인 종목이 없습니다</div>';
  h+='</div>';
  if(N.C>0.5||R.others.length){
    h+=`<h2 class="sec-title">현금·기타 자산</h2><div class="list">${N.C>0.5?`<div class="row"><div><div class="nm2">증권계좌 예수금</div><div class="sub">입금·매매 기록으로 계산</div></div><span></span><b>${KRWs(N.C)}원</b></div>`:''}
      ${R.others.map(o=>{const g=(+o.last.val)-(+o.last.prin||0);return `<div class="row"><div><div class="nm2">${esc(o.name)} <span class="mute" style="font-size:12px">${esc(o.type)}</span></div><div class="sub">${md(o.last.d)} 기준 · 납입 ${KRWs(+o.last.prin||0)}원${+o.last.prin?` · <span class="${sgnCls(g)}">${g>=0?'+':''}${KRWs(g)}원</span>`:''}</div></div><span></span><b>${KRWs(+o.last.val)}원</b></div>`;}).join('')}</div>`;
  }
  h+=pfMissing(R.hold.map(x=>x.k));
  h+=`<p class="note">시세는 하루 4번 받습니다(국내 장중에는 수집 시점 가격). 미국 종목은 그날 원/달러 환율로 바꿔 더합니다. PER·PBR·배당·ROE는 네이버 증권(국내)·야후 파이낸스(미국) 기준이고, 해자는 Claude가 ${esc(MO?.asof||'')} 기준으로 정리한 정성 의견입니다. 투자 권유가 아닙니다.</p>`;
  el.innerHTML=h;
  $$('#pfAllocSeg button').forEach(b=>b.onclick=()=>{pfAlloc=b.dataset.v;store.set('pfAlloc',pfAlloc);pfDraw();});
  $$('[data-hk]').forEach(b=>b.onclick=()=>{pfOpen[b.dataset.hk]=!pfOpen[b.dataset.hk];pfDraw();});
  bindMissing(el);
}
function pfMissing(have){
  const miss=(D?.stocks||[]).filter(s=>s.group==='portfolio'&&!have.includes(s.id));
  if(!miss.length) return '';
  return `<h2 class="sec-title">매수 기록이 없는 보유 종목</h2><p class="note">보유 목록에는 있지만 매수 기록이 없어 비중·수익률에서 빠진 종목입니다. 누르면 매수 기록 입력으로 갑니다.</p>
    <div class="chips">${miss.map(s=>`<button class="chip-b" data-miss="${esc(s.id)}">${esc(s.name)} +</button>`).join('')}</div>`;
}
function bindMissing(el){
  el.querySelectorAll('[data-miss]').forEach(b=>b.onclick=()=>{recKind='B';pfSeg='rec';store.set('pfSeg',pfSeg);pfDraw();const sel=$('#rStock');if(sel){sel.value=b.dataset.miss;sel.dispatchEvent(new Event('change'));}$('#rQty')?.focus();});
}

/* ---------- 수익률 ---------- */
function drawReturn(el,R){
  if(!R||R.rows.length<2){el.innerHTML='<div class="empty">수익률을 그리려면 최소 이틀치 기록이 필요합니다. 과거 매수일로 기록을 넣으면 그날부터 그려집니다.</div>';return;}
  const all=R.rows, from=pfPer==='all'?all[0].d:kstParts(new Date(Date.now()-(+pfPer)*864e5)).date;
  let rows=all.filter(r=>r.d>=from); if(rows.length<2) rows=all.slice(-2);
  const chain=rs=>rs.reduce((a,r)=>r.r==null?a:a*(1+r.r),1)-1;
  const pr=chain(rows.slice(1)), pl=rows.slice(1).reduce((a,r)=>a+r.pl,0);
  const first=rows[0], last=rows[rows.length-1];
  let h=`<div class="seg" role="group" aria-label="기간" id="pfPerSeg">${[['30','1개월'],['90','3개월'],['365','1년'],['all','전체']].map(([v,l])=>`<button data-v="${v}" aria-pressed="${pfPer===v}">${l}</button>`).join('')}</div>
    <div class="sum" style="margin-top:10px"><div>기간 수익률<b class="${sgnCls(pr)}">${PCT(pr)}</b></div><div>기간 손익<b class="${sgnCls(pl)}">${pl>=0?'+':''}${KRWs(pl)}</b></div><div>평가액<b>${KRWs(last.V)}</b></div></div>
    <div class="chart-box"><div class="legend"><span><i class="sw s1"></i>평가액</span><span><i class="sw s0"></i>원금</span></div><div id="pfChart" class="chart"></div></div>`;
  // 일·월·연
  const groups=[]; const keyOf=d=>pfGrain==='d'?d:(pfGrain==='m'?d.slice(0,7):d.slice(0,4));
  const src=pfGrain==='d'?rows.slice(1):all.slice(1);
  src.forEach(r=>{const k=keyOf(r.d);let g=groups[groups.length-1];if(!g||g.k!==k){g={k,rs:[]};groups.push(g);}g.rs.push(r);});
  const G=groups.map(g=>({k:g.k,r:chain(g.rs),pl:g.rs.reduce((a,r)=>a+r.pl,0),V:g.rs[g.rs.length-1].V})).reverse().slice(0,pfGrain==='d'?60:36);
  h+=`<h2 class="sec-title">기간별 수익률</h2><div class="seg" role="group" aria-label="단위" id="pfGrainSeg">${[['d','일별'],['m','월별'],['y','연별']].map(([v,l])=>`<button data-v="${v}" aria-pressed="${pfGrain===v}">${l}</button>`).join('')}</div>
    <div id="pfBars" class="chart bars"></div>
    <div class="list" style="margin-top:8px"><div class="rt rt-h"><span>기간</span><span>수익률</span><span>손익</span><span>평가액</span></div>${G.map(g=>`<div class="rt"><span>${pfGrain==='d'?md(g.k):(pfGrain==='m'?`${g.k.slice(2,4)}.${g.k.slice(5)}`:g.k+'년')}</span><span class="${sgnCls(g.r)}"><b>${PCT(g.r)}</b></span><span class="${sgnCls(g.pl)}">${g.pl>=0?'+':''}${KRWs(g.pl)}</span><span>${KRWs(g.V)}</span></div>`).join('')}</div>
    <p class="note">수익률은 입금·출금 효과를 뺀 시간가중 수익률입니다(날마다 수익률을 이어 곱함). 손익은 평가액 변화에서 들어오고 나간 원금을 뺀 금액입니다. 주말은 건너뛰고, 휴장일은 직전 종가로 계산합니다.</p>`;
  el.innerHTML=h;
  $$('#pfPerSeg button').forEach(b=>b.onclick=()=>{pfPer=b.dataset.v;store.set('pfPer',pfPer);pfDraw();});
  $$('#pfGrainSeg button').forEach(b=>b.onclick=()=>{pfGrain=b.dataset.v;store.set('pfGrain',pfGrain);pfDraw();});
  lineChart($('#pfChart'),rows);
  barChart($('#pfBars'),[...G].reverse().slice(-(pfGrain==='d'?30:24)));
}
function niceTicks(lo,hi,n=4){const span=hi-lo||Math.abs(hi)||1, step0=span/n, mag=Math.pow(10,Math.floor(Math.log10(step0))), f=step0/mag, step=(f<1.5?1:f<3?2:f<7?5:10)*mag;
  const t=[];for(let v=Math.ceil(lo/step)*step;v<=hi+step*1e-6;v+=step)t.push(v);return t;}
function lineChart(el,rows){
  const W=el.clientWidth||358, H=210, L=46, Rm=8, T=10, B=24;
  const vs=rows.flatMap(r=>[r.V,r.P]), lo0=Math.min(...vs), hi0=Math.max(...vs), pad=(hi0-lo0)*0.08||hi0*0.05||1;
  const ticks=niceTicks(lo0-pad,hi0+pad), lo=Math.min(ticks[0],lo0-pad), hi=Math.max(ticks[ticks.length-1],hi0+pad);
  const x=i=>L+(W-L-Rm)*(rows.length<2?0:i/(rows.length-1)), y=v=>T+(H-T-B)*(1-(v-lo)/(hi-lo||1));
  const pV=rows.map((r,i)=>`${i?'L':'M'}${x(i).toFixed(1)},${y(r.V).toFixed(1)}`).join('');
  let pP='';rows.forEach((r,i)=>{pP+=i?`H${x(i).toFixed(1)}V${y(r.P).toFixed(1)}`:`M${x(0).toFixed(1)},${y(r.P).toFixed(1)}`;});
  const area=pV+`L${x(rows.length-1).toFixed(1)},${(H-B)}L${x(0).toFixed(1)},${H-B}Z`;
  const xt=[0,Math.floor((rows.length-1)/2),rows.length-1].filter((v,i,a)=>a.indexOf(v)===i);
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="평가액과 원금 추이">
    ${ticks.map(t=>`<line x1="${L}" x2="${W-Rm}" y1="${y(t)}" y2="${y(t)}" class="grid"/><text x="${L-6}" y="${y(t)+4}" class="ax" text-anchor="end">${KRWs(t)}</text>`).join('')}
    ${xt.map(i=>`<text x="${x(i)}" y="${H-6}" class="ax" text-anchor="${i===0?'start':i===rows.length-1?'end':'middle'}">${rows[i].d.slice(2).replace(/-/g,'.')}</text>`).join('')}
    <path d="${area}" class="area"/><path d="${pP}" class="ln0"/><path d="${pV}" class="ln1"/>
    <line class="xh" y1="${T}" y2="${H-B}" x1="-10" x2="-10"/><circle class="dt" r="4.5" cx="-10" cy="-10"/>
    <rect x="${L}" y="0" width="${W-L-Rm}" height="${H}" fill="transparent" class="hit"/></svg><div class="tip" hidden></div>`;
  const svg=el.querySelector('svg'), tip=el.querySelector('.tip'), xh=el.querySelector('.xh'), dt=el.querySelector('.dt');
  const move=ev=>{const b=svg.getBoundingClientRect(), px=(ev.clientX-b.left)*W/b.width;
    const i=Math.max(0,Math.min(rows.length-1,Math.round((px-L)/((W-L-Rm)/(rows.length-1||1))))), r=rows[i];
    xh.setAttribute('x1',x(i));xh.setAttribute('x2',x(i));dt.setAttribute('cx',x(i));dt.setAttribute('cy',y(r.V));
    const g=r.V-r.P;
    tip.hidden=false;tip.innerHTML=`<b>${r.d.replace(/-/g,'.')}</b><div>평가액 <b>${KRW(r.V)}</b></div><div>원금 ${KRW(r.P)}</div><div class="${sgnCls(g)}">손익 ${g>=0?'+':''}${KRW(g)} (${PCT(r.P>0?g/r.P:null)})</div>`;
    const tx=x(i)/W*b.width; tip.style.left=Math.min(b.width-tip.offsetWidth-4,Math.max(4,tx-tip.offsetWidth/2))+'px';};
  const hide=()=>{tip.hidden=true;xh.setAttribute('x1',-10);xh.setAttribute('x2',-10);dt.setAttribute('cx',-10);};
  svg.addEventListener('pointermove',move);svg.addEventListener('pointerdown',move);svg.addEventListener('pointerleave',hide);
}
function barChart(el,G){
  if(!G.length){el.innerHTML='';return;}
  const W=el.clientWidth||358, H=150, L=40, Rm=6, T=8, B=20;
  const vs=G.map(g=>g.r), mx=Math.max(0.001,...vs.map(Math.abs)), ticks=niceTicks(-mx,mx,4), lo=Math.min(ticks[0],-mx), hi=Math.max(ticks[ticks.length-1],mx);
  const y=v=>T+(H-T-B)*(1-(v-lo)/(hi-lo)), bw=(W-L-Rm)/G.length, gap=Math.min(4,bw*0.25);
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="기간별 수익률 막대">
    ${ticks.map(t=>`<line x1="${L}" x2="${W-Rm}" y1="${y(t)}" y2="${y(t)}" class="grid${Math.abs(t)<1e-12?' zero':''}"/><text x="${L-5}" y="${y(t)+4}" class="ax" text-anchor="end">${(t*100).toFixed(Math.abs(hi)<0.02?1:0)}%</text>`).join('')}
    ${G.map((g,i)=>{const x0=L+i*bw+gap/2,w=Math.max(1,bw-gap),y0=y(Math.max(0,g.r)),y1=y(Math.min(0,g.r)),hgt=Math.max(1,y1-y0),rr=Math.min(3,w/2,hgt);
      const top=g.r>=0;
      const d=top?`M${x0},${y1}V${y0+rr}Q${x0},${y0} ${x0+rr},${y0}H${x0+w-rr}Q${x0+w},${y0} ${x0+w},${y0+rr}V${y1}Z`:`M${x0},${y0}V${y1-rr}Q${x0},${y1} ${x0+rr},${y1}H${x0+w-rr}Q${x0+w},${y1} ${x0+w},${y1-rr}V${y0}Z`;
      return `<path d="${d}" class="${g.r>=0?'bu':'bd'}"/><rect x="${L+i*bw}" y="${T}" width="${bw}" height="${H-T-B}" fill="transparent" data-i="${i}"/>`;}).join('')}
    <text x="${L}" y="${H-5}" class="ax">${lbl(G[0].k)}</text><text x="${W-Rm}" y="${H-5}" class="ax" text-anchor="end">${lbl(G[G.length-1].k)}</text></svg><div class="tip" hidden></div>`;
  function lbl(k){return k.length===10?k.slice(5).replace('-','/'):(k.length===7?k.slice(2).replace('-','.'):k+'년');}
  const tip=el.querySelector('.tip'), svg=el.querySelector('svg');
  svg.querySelectorAll('[data-i]').forEach(r=>{const sh=()=>{const g=G[+r.dataset.i],b=svg.getBoundingClientRect();
    tip.hidden=false;tip.innerHTML=`<b>${lbl(g.k)}</b><div class="${sgnCls(g.r)}">${PCT(g.r)} · ${g.pl>=0?'+':''}${KRWs(g.pl)}원</div>`;
    const cx=(+r.getAttribute('x')+bw/2)/W*b.width;tip.style.left=Math.min(b.width-tip.offsetWidth-4,Math.max(4,cx-tip.offsetWidth/2))+'px';};
    r.addEventListener('pointerenter',sh);r.addEventListener('pointerdown',sh);});
  svg.addEventListener('pointerleave',()=>tip.hidden=true);
}

/* ---------- 기록 ---------- */
function stockOptions(p){
  const m=new Map();
  (D?.stocks||[]).forEach(s=>m.set(s.id,{k:s.id,name:s.name,g:s.group}));
  Object.keys(PX?.px||{}).forEach(k=>{if(!m.has(k))m.set(k,{k,name:nameOf(k),g:'prices'});});
  p.trades.forEach(t=>{if(!m.has(t.k))m.set(t.k,{k:t.k,name:t.name||nameOf(t.k),g:'trade'});});
  const order={portfolio:0,prices:1,trade:1,watch:2};
  return [...m.values()].sort((a,b)=>(order[a.g]-order[b.g])||a.name.localeCompare(b.name,'ko'));
}
function drawRecord(el,p,R){
  const K=[['B','매수'],['S','매도'],['in','입금'],['out','출금'],['inc','배당·이자'],['oth','기타 자산']];
  const opts=stockOptions(p);
  let f='';
  if(recKind==='B'||recKind==='S'){
    f=`<label>종목<select class="inp" id="rStock">${opts.map(o=>`<option value="${esc(o.k)}">${esc(o.name)} (${esc(o.k.replace(':',' '))})${o.g==='watch'?' · 관심':''}</option>`).join('')}<option value="__new">+ 다른 종목·ETF 직접 입력</option></select></label>
      <div id="rNew" hidden><div class="seg" role="group" id="rMkt" style="margin-top:10px"><button data-v="KR" aria-pressed="true">국내</button><button data-v="US" aria-pressed="false">미국</button></div>
        <div class="grid2"><label>종목코드·티커<input class="inp" id="rCode" autocomplete="off" placeholder="예: 233740 / NVDA"></label><label>이름<input class="inp" id="rName" autocomplete="off" placeholder="예: KODEX 코스닥150레버리지"></label></div>
        <div class="seg" role="group" id="rTrack" style="margin-top:8px"><button data-v="prices" aria-pressed="true">시세만 추적 (ETF 등)</button><button data-v="portfolio" aria-pressed="false">보유 종목에 추가 (뉴스도)</button></div></div>
      <div class="grid2"><label>날짜<input class="inp" type="date" id="rDate" value="${today()}" max="${today()}"></label><label>수량 (주)<input class="inp" id="rQty" inputmode="decimal" autocomplete="off"></label></div>
      <div class="grid2"><label><span id="rPxL">1주 가격 (원)</span><input class="inp" id="rPx" inputmode="decimal" autocomplete="off"></label><label>수수료·세금 (원, 선택)<input class="inp" id="rFee" inputmode="decimal" autocomplete="off" placeholder="0"></label></div>
      <label id="rFxW" hidden>환율 (원/달러, 비우면 그날 환율)<input class="inp" id="rFx" inputmode="decimal" autocomplete="off"></label>
      <div class="msg mute" id="rHint"></div>`;
  }else if(recKind==='oth'){
    f=`<label>자산<select class="inp" id="oSel">${p.others.map(o=>`<option value="${esc(o.id)}">${esc(o.name)} (${esc(o.type)})</option>`).join('')}<option value="__new">+ 새 자산</option></select></label>
      <div id="oNew"><div class="grid2"><label>이름<input class="inp" id="oName" placeholder="예: 우리은행 정기예금"></label><label>종류<select class="inp" id="oType">${['예금','적금','CMA','펀드','연금','기타'].map(t=>`<option>${t}</option>`).join('')}</select></label></div></div>
      <div class="grid2"><label>기준일<input class="inp" type="date" id="oDate" value="${today()}" max="${today()}"></label><label>현재 평가액 (원)<input class="inp" id="oVal" inputmode="decimal"></label></div>
      <label>지금까지 넣은 원금 (원)<input class="inp" id="oPrin" inputmode="decimal" placeholder="예금이면 맡긴 금액"></label>
      <p class="note">값이 바뀔 때마다(이자·납입·평가액 변동) 새 기준일로 다시 넣으면 그래프에 반영됩니다.</p>`;
  }else{
    f=`<div class="grid2"><label>날짜<input class="inp" type="date" id="fDate" value="${today()}" max="${today()}"></label><label>금액 (원)<input class="inp" id="fAmt" inputmode="decimal"></label></div>
      <label>메모 (선택)<input class="inp" id="fMemo" autocomplete="off"></label>
      <p class="note">${recKind==='inc'?'배당금·이자처럼 원금이 아닌 수익으로 들어온 돈입니다 (예수금은 늘고 원금은 그대로).':recKind==='in'?'증권계좌에 넣은 투자 원금입니다.':'증권계좌에서 뺀 돈입니다 (원금에서 빠짐).'}</p>`;
  }
  const recs=[...p.trades.map(t=>({d:t.d,id:t.id,typ:'t',x:t})),...p.flows.map(x=>({d:x.d,id:x.id,typ:'f',x})),...p.others.flatMap(o=>o.snaps.map(s=>({d:s.d,id:s.id,typ:'o',x:s,o})))].sort((a,b)=>a.d<b.d?1:a.d>b.d?-1:0);
  const recHtml=r=>{
    if(r.typ==='t'){const t=r.x,us=isUS(t.k);return `<div class="row"><div><div class="nm2"><span class="k ${t.side==='B'?'buy':'sell'}">${t.side==='B'?'매수':'매도'}</span> ${esc(t.name||nameOf(t.k))}</div><div class="sub">${t.d} · ${(+t.q).toLocaleString('ko-KR')}주 × ${us?'$'+(+t.p).toFixed(2):(+t.p).toLocaleString('ko-KR')+'원'}${t.fee?` · 수수료 ${(+t.fee).toLocaleString('ko-KR')}원`:''}</div></div><span></span><button class="btn sm del" data-del="t:${esc(t.id)}">삭제</button></div>`;}
    if(r.typ==='f'){const f=r.x,L={in:'입금',out:'출금',inc:'배당·이자'};return `<div class="row"><div><div class="nm2"><span class="k ${f.kind==='out'?'sell':'neu'}">${L[f.kind]}</span> ${KRW(+f.amt)}</div><div class="sub">${f.d}${f.memo?' · '+esc(f.memo):''}</div></div><span></span><button class="btn sm del" data-del="f:${esc(f.id)}">삭제</button></div>`;}
    const s=r.x;return `<div class="row"><div><div class="nm2"><span class="k trust">${esc(r.o.type)}</span> ${esc(r.o.name)}</div><div class="sub">${s.d} · 평가 ${KRW(+s.val)} · 원금 ${KRW(+s.prin||0)}</div></div><span></span><button class="btn sm del" data-del="o:${esc(r.o.id)}:${esc(s.id)}">삭제</button></div>`;
  };
  el.innerHTML=`<div class="form"><div class="chips" role="group" aria-label="기록 종류" id="rKind" style="margin-top:0">${K.map(([v,l])=>`<button class="chip-b" data-v="${v}" aria-pressed="${recKind===v}">${l}</button>`).join('')}</div>
    ${f}<div class="btns"><button class="btn pri" id="rSave">기록 추가</button></div><div class="msg" id="rMsg"></div></div>
    <label class="tog"><input type="checkbox" id="pfAuto" ${p.auto?'checked':''}> 모자란 매수금은 입금으로 보기 <small>입금 기록을 따로 넣지 않을 때 켜 두세요. 매수에 쓴 돈이 원금으로 잡힙니다.</small></label>
    ${R&&R.autoIn>0.5&&p.auto?`<p class="note">지금까지 자동으로 원금 처리된 금액: <b>${KRW(R.autoIn)}</b></p>`:''}
    <h2 class="sec-title">기록 <span class="mute" style="font-size:14px">${recs.length}건</span></h2>
    <div class="list">${recs.slice(0,pfOpen.__all?9999:40).map(recHtml).join('')||'<div class="empty" style="border-radius:0">아직 기록이 없습니다</div>'}</div>
    ${recs.length>40&&!pfOpen.__all?`<button class="more" id="rAll" style="background:var(--surface);border-radius:0 0 14px 14px">${recs.length-40}건 더 보기</button>`:''}
    <h2 class="sec-title">백업</h2>
    <div class="form"><p class="note" style="margin-top:0">기록은 <b>이 폰의 이 브라우저에만</b> 저장됩니다. 브라우저 데이터를 지우거나 폰을 바꾸면 사라지니 가끔 파일로 내보내 두세요. 다른 기기에서는 같은 파일을 가져오면 됩니다.</p>
      <div class="btns"><button class="btn" id="bkOut">파일로 내보내기</button><button class="btn" id="bkIn">파일에서 가져오기</button><button class="btn del" id="bkClr">모두 지우기</button></div>
      <input type="file" id="bkFile" accept="application/json,.json" hidden><div class="msg" id="bkMsg"></div></div>`;
  // 종류
  $$('#rKind button').forEach(b=>b.onclick=()=>{recKind=b.dataset.v;pfDraw();});
  $('#pfAuto').onchange=e=>{const q=pfGet();q.auto=e.target.checked;pfSet(q);pfDraw();};
  $('#rAll')&&($('#rAll').onclick=()=>{pfOpen.__all=true;pfDraw();});
  let rMkt='KR', rTrack='prices';
  const sel=$('#rStock');
  const syncStock=()=>{
    const v=sel.value, isNew=v==='__new', us=isNew?rMkt==='US':isUS(v);
    $('#rNew').hidden=!isNew; $('#rFxW').hidden=!us; $('#rPxL').textContent=us?'1주 가격 (달러)':'1주 가격 (원)';
    const hint=$('#rHint');
    if(!isNew){const hold=R?.hold.find(h=>h.k===v);const c=lastLE(series(v),d2i($('#rDate').value||today()));
      hint.textContent=[hold?`보유 ${(+hold.q).toLocaleString('ko-KR')}주`:'',c!=null?`그날 종가 ${us?'$'+c.toFixed(2):Math.round(c).toLocaleString('ko-KR')+'원'}`:(series(v)?'':'아직 시세가 없는 종목입니다 (다음 수집 후 반영)')].filter(Boolean).join(' · ');
      if(c!=null&&!$('#rPx').value) $('#rPx').placeholder=us?c.toFixed(2):String(Math.round(c));}
    else hint.textContent='';
  };
  if(sel){
    sel.onchange=syncStock; $('#rDate').onchange=()=>{$('#rPx').placeholder='';syncStock();};
    $$('#rMkt button').forEach(b=>b.onclick=()=>{rMkt=b.dataset.v;setSeg('#rMkt',rMkt);syncStock();});
    $$('#rTrack button').forEach(b=>b.onclick=()=>{rTrack=b.dataset.v;setSeg('#rTrack',rTrack);});
    syncStock();
  }
  const oSel=$('#oSel');
  if(oSel){const sync=()=>{$('#oNew').hidden=oSel.value!=='__new';const o=p.others.find(x=>x.id===oSel.value);if(o){const l=o.snaps[o.snaps.length-1];$('#oPrin').value=l?.prin||'';}};oSel.onchange=sync;sync();}
  // 저장
  const n=v=>{const x=parseFloat(String(v||'').replace(/[,\s원$]/g,''));return isFinite(x)?x:NaN;};
  $('#rSave').onclick=async()=>{
    const m=$('#rMsg'); m.className='msg err'; const q=pfGet();
    if(recKind==='B'||recKind==='S'){
      let k=sel.value, name;
      if(k==='__new'){
        const code=$('#rCode').value.trim().toUpperCase(); name=$('#rName').value.trim()||code;
        if(rMkt==='KR'&&!/^[0-9A-Z]{6}$/.test(code)){m.textContent='국내 종목·ETF는 6자리 코드를 넣어 주세요 (예: 233740).';return;}
        if(rMkt==='US'&&!/^[A-Z][A-Z0-9.\-]{0,7}$/.test(code)){m.textContent='미국 종목은 티커를 넣어 주세요 (예: NVDA).';return;}
        k=`${rMkt}:${code}`;
      } else name=nameOf(k);
      const d=$('#rDate').value, qty=n($('#rQty').value), px=n($('#rPx').value||$('#rPx').placeholder), fee=n($('#rFee').value)||0, fx=n($('#rFx')?.value);
      if(!d){m.textContent='날짜를 넣어 주세요.';return;}
      if(!(qty>0)){m.textContent='수량을 넣어 주세요.';$('#rQty').focus();return;}
      if(!(px>0)){m.textContent='가격을 넣어 주세요.';$('#rPx').focus();return;}
      const t={id:uid(),d,k,name,side:recKind,q:qty,p:px,fee}; if(isUS(k)&&fx>0) t.fx=fx;
      q.trades.push(t); pfSet(q);
      m.className='msg ok'; m.textContent=`${d} ${name} ${qty}주 ${recKind==='B'?'매수':'매도'} 기록을 넣었습니다.`;
      if(sel.value==='__new'&&!series(k)) await addTicker(k,name,rTrack,m);
      $('#rQty').value='';$('#rPx').value='';$('#rFee').value='';
      setTimeout(pfDraw,1200);
    }else if(recKind==='oth'){
      let id=oSel.value, o;
      if(id==='__new'){const name=$('#oName').value.trim();if(!name){m.textContent='자산 이름을 넣어 주세요.';return;}o={id:uid(),name,type:$('#oType').value,snaps:[]};q.others.push(o);}
      else o=q.others.find(x=>x.id===id);
      const val=n($('#oVal').value), prin=n($('#oPrin').value);
      if(!(val>=0)){m.textContent='평가액을 넣어 주세요.';return;}
      o.snaps=o.snaps.filter(s=>s.d!==$('#oDate').value);
      o.snaps.push({id:uid(),d:$('#oDate').value,val,prin:isFinite(prin)?prin:(o.snaps[o.snaps.length-1]?.prin||0)});
      o.snaps.sort((a,b)=>a.d<b.d?-1:1); pfSet(q); pfDraw();
    }else{
      const amt=n($('#fAmt').value); if(!(amt>0)){m.textContent='금액을 넣어 주세요.';return;}
      q.flows.push({id:uid(),d:$('#fDate').value,kind:recKind,amt,memo:$('#fMemo').value.trim()}); pfSet(q); pfDraw();
    }
  };
  el.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>{
    const [t,id,sid]=b.dataset.del.split(':'), q=pfGet();
    if(b.dataset.armed!=='1'){b.dataset.armed='1';b.textContent='정말 삭제';setTimeout(()=>{if(b.isConnected){b.dataset.armed='';b.textContent='삭제';}},3000);return;}
    if(t==='t') q.trades=q.trades.filter(x=>x.id!==id);
    else if(t==='f') q.flows=q.flows.filter(x=>x.id!==id);
    else {const o=q.others.find(x=>x.id===id); if(o){o.snaps=o.snaps.filter(s=>s.id!==sid); if(!o.snaps.length) q.others=q.others.filter(x=>x.id!==id);}}
    pfSet(q); pfDraw();
  });
  // 백업
  $('#bkOut').onclick=()=>{const blob=new Blob([JSON.stringify(pfGet(),null,1)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`내자산-기록-${today()}.json`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),2000);};
  $('#bkIn').onclick=()=>$('#bkFile').click();
  $('#bkFile').onchange=async e=>{const f=e.target.files[0];if(!f)return;const m=$('#bkMsg');
    try{const j=JSON.parse(await f.text());if(!Array.isArray(j.trades))throw new Error('기록 파일이 아닙니다');
      pfSet({v:1,auto:j.auto!==false,trades:j.trades,flows:j.flows||[],others:j.others||[]});pfDraw();const m2=$('#bkMsg');m2.className='msg ok';m2.textContent=`가져왔습니다: 매매 ${j.trades.length}건, 입출금 ${(j.flows||[]).length}건, 기타 자산 ${(j.others||[]).length}개`;}
    catch(err){m.className='msg err';m.textContent='가져오기 실패: '+err.message;}};
  $('#bkClr').onclick=()=>{const b=$('#bkClr');if(b.dataset.armed!=='1'){b.dataset.armed='1';b.textContent='정말 모두 지우기';return;}pfSet({v:1,auto:true,trades:[],flows:[],others:[]});pfDraw();};
}
async function addTicker(k,name,group,m){
  const [mkt,code]=k.split(':');
  if(!token()){m.innerHTML+='<br>이 종목은 아직 시세를 받지 않습니다. <b>목록</b>의 GitHub 연결을 하면 자동으로 시세 추적에 추가됩니다.';return;}
  try{
    const r=await api('contents/stocks.json?t='+Date.now(),{cache:'no-store'}); if(!r.ok) throw new Error('HTTP '+r.status);
    const j=await r.json(), obj=JSON.parse(b64dec(j.content));
    const has=g=>(obj[g]||[]).some(s=>(s.market||'KR')===mkt&&(s.code||'').toUpperCase()===code);
    if(!has('portfolio')&&!has('watch')&&!has('prices')){
      obj[group]=obj[group]||[]; obj[group].push({market:mkt,name,code});
      const w=await api('contents/stocks.json',{method:'PUT',body:JSON.stringify({message:`${group==='prices'?'시세 추적':'보유 종목'} 추가: ${name} (앱에서)`,content:b64enc(JSON.stringify(obj,null,2)+'\n'),sha:j.sha})});
      if(!w.ok) throw new Error('HTTP '+w.status);
      WORK=null;  // 목록 화면 다시 읽기
    }
    m.innerHTML+=`<br>${esc(name)}을(를) ${group==='prices'?'시세 추적':'보유 종목'}에 추가했습니다. 2~3분 뒤 시세가 반영됩니다.`;
  }catch(e){m.innerHTML+=`<br>시세 추적 추가 실패(${esc(e.message||e)}) — 목록 탭에서 직접 추가해 주세요.`;}
}

/* ---------- 화면 ---------- */
async function pfDraw(){
  setSeg('#mgSeg',pfSeg);
  ['asset','ret','rec','list'].forEach(s=>{$('#mg-'+s).hidden=s!==pfSeg;});
  $('#ttl').textContent=pfSeg==='list'?'종목 관리':'내 자산';
  if(pfSeg==='list'){ if(!WORK) loadList(); else drawManage(); drawStatus(); return; }
  const el=$('#mg-'+pfSeg);
  if(!PX&&!MK){el.innerHTML='<div class="loading">불러오는 중…</div>'; await pfLoad();}
  const p=pfGet(); let R=null;
  try{R=pfCalc(p);}catch(e){console.error(e);el.innerHTML=`<div class="warn">계산 중 오류: ${esc(e.message)}</div>`;return;}
  if(!PX) el.insertAdjacentHTML('afterbegin','<div class="warn">시세 파일을 아직 받지 못했습니다. 다음 수집 뒤에 다시 열어 주세요.</div>');
  if(pfSeg==='asset') drawAsset(el,R,p); else if(pfSeg==='ret') drawReturn(el,R); else drawRecord(el,p,R);
}
function pfShow(){pfDraw();}
$$('#mgSeg button').forEach(b=>b.onclick=()=>{pfSeg=b.dataset.v;store.set('pfSeg',pfSeg);pfDraw();scrollTo(0,0);});
window.addEventListener('resize',()=>{if(view==='manage'&&pfSeg==='ret')pfDraw();});
