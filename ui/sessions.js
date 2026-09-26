/* The existing session connects itself. Chat has one input and one send action.
   A live session follows the chat project automatically; a stuck job can be cancelled here. */
(() => {
  let state=null, token='', flight=null, bindAttempt={key:'',t:0};
  const labels={queued:'Wartet',working:'In Bearbeitung',question:'Rückfrage',review:'Wird geprüft',
    done:'Beantwortet',cancelled:'Abgebrochen',interrupted:'Unterbrochen',failed:'Fehlgeschlagen',conflict:'Versionskonflikt'};
  const style=document.createElement('style');
  style.textContent='.msg.codex,.msg.agent{align-self:flex-start;background:var(--panel)} #jobCancel{font-size:10.5px;padding:0 6px;line-height:1.5;flex:0 0 auto}';
  document.head.append(style);
  async function api(action,data,retry=true){
    if(data!==undefined&&!token){
      const r=await fetch('/api/session');if(!r.ok)throw new Error('Werkbank nicht erreichbar.');
      token=(await r.json()).token;
    }
    const r=await fetch('/api/agents/'+action,data===undefined?{}:{method:'POST',
      headers:{'Content-Type':'application/json','X-Werkbank-Token':token},body:JSON.stringify(data)});
    if(r.status===401&&retry){token='';return api(action,data,false);}
    const result=await r.json();if(!r.ok)throw new Error(result.message||'Senden fehlgeschlagen.');
    return result;
  }
  const live=s=>!!(s&&s.online&&s.verified&&!s.detached);
  function candidateSession(){
    if(!state)return null;
    const armed=state.sessions.filter(s=>live(s)&&s.wake_armed);
    const bound=armed.filter(s=>(s.bound||[]).length);
    // Prefer the session that already serves another project, then the most recently registered one.
    return (bound.length?bound:armed).sort((a,b)=>String(b.registered).localeCompare(String(a.registered)))[0]||null;
  }
  async function autoBind(){
    if(!state||typeof chatP==='undefined'||!chatP||(state.bindings[chatP]||{}).enabled)return;
    const s=candidateSession();if(!s)return;
    const key=chatP+'|'+s.id, now=Date.now();
    if(bindAttempt.key===key&&now-bindAttempt.t<5000)return;
    bindAttempt={key,t:now};
    try{await api('bind',{project:chatP,session:s.id,auto:true});await refresh();}
    catch(_){/* shown by the next state refresh */}
  }
  function cancelButton(active){
    let b=document.getElementById('jobCancel');
    if(!active){if(b)b.remove();return;}
    if(!b){b=document.createElement('button');b.id='jobCancel';b.title='Diesen Auftrag abbrechen; die Sitzung bleibt verbunden';
      b.textContent='abbrechen';document.getElementById('status').insertAdjacentElement('afterend',b);}
    b.onclick=async()=>{b.disabled=true;try{await api('cancel',{job:active.id});await refresh();}
      catch(e){if(typeof alertZeile==='function')alertZeile('Abbruch fehlgeschlagen: '+e.message);}finally{b.disabled=false;}};
  }
  function render(){
    if(!state||typeof chatP==='undefined')return;
    const jobLabels=Object.fromEntries(Object.entries(state.jobs).map(([id,j])=>[id,
      (labels[j.state]||j.state)+(j.reason?' · '+j.reason:'')]));
    if(JSON.stringify(jobLabels)!==JSON.stringify(window.werkbankJobLabels)){
      window.werkbankJobLabels=jobLabels;if(typeof chatRender==='function')chatRender();
    }
    const binding=(chatP&&state.bindings[chatP])||{}, session=state.sessions.find(s=>s.id===binding.session);
    const active=chatP?Object.values(state.jobs).find(j=>j.project===chatP&&['working','question','review'].includes(j.state)):null;
    const bound=!!(session&&binding.enabled);
    const ready=!!(bound&&live(session)&&(session.wake_armed||active));
    const reach=active?.state==='working'?'arbeitet':active?.state==='question'?'wartet auf deine Antwort':active?.state==='review'?'Ergebnis wird geprüft'
      :ready?'bereit':'Verbindung inaktiv · uiworkbench erneut aufrufen';
    let text;
    if(bound)text=`${session.name} · ${reach}`;
    else{const c=candidateSession();
      text=c?(chatP?`${c.name} · wird verbunden …`:`${c.name} · verbunden, wartet auf ein Projekt`):'Sitzung verbinden: Claude /uiworkbench · Codex $uiworkbench';}
    window.werkbankTerminalStatus=text;
    window.werkbankTerminalReady=ready;
    window.werkbankTerminalBound=bound;
    document.getElementById('text').placeholder=active?.state==='question'?'Antwort schreiben …':'Nachricht schreiben …';
    cancelButton(active);
    if(typeof statusPruefen==='function')statusPruefen();
    autoBind();
  }
  async function refresh(){
    if(flight)return flight;
    flight=(async()=>{
      try{state=await api('state');render();}
      catch(_){window.werkbankTerminalReady=false;window.werkbankTerminalStatus='Werkbank nicht erreichbar';
        if(typeof statusPruefen==='function')statusPruefen();}
    })();
    try{await flight;}finally{flight=null;}
  }
  window.werkbankQuestion=async(project)=>{
    await refresh();
    const question=state&&Object.values(state.jobs).find(j=>j.project===project&&j.state==='question');
    return question?.id||null;
  };
  window.werkbankRefresh=refresh;
  refresh();setInterval(refresh,900);
})();
