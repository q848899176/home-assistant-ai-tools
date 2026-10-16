const esc = (value = "") => String(value).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const time = value => value ? new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai", dateStyle:"medium", timeStyle:"short"}).format(new Date(value)) : "—";
const labels = {pending:"等待提醒",delivering:"正在播报",completed:"已完成",cancelled:"已取消",failed:"播报失败",missed:"已错过"};
const historyStatuses = new Set(["completed","cancelled","failed","missed"]);
const profileDefaults={name:"AI 家庭助手",connection_id:"",chat_model:"gpt-4o-mini",prompt:"你是 {{ ha_name }} 的家庭助手。使用提供的设备状态与工具真实结果，简短地用中文回答。当前北京时间：{{ now() }}。",functions:"[]",max_tokens:2000,top_p:1,temperature:0.5,max_function_calls_per_conversation:8,attach_username:false,use_tools:true,context_threshold:13000,context_truncate_strategy:"clear"};
const repeats = {none:"仅一次",daily:"每天",weekdays:"工作日",weekly:"每周"};
const conditions = {"clear-night":"晴朗夜晚",cloudy:"多云",exceptional:"异常天气",fog:"有雾",hail:"冰雹",lightning:"雷电","lightning-rainy":"雷阵雨",partlycloudy:"局部多云",pouring:"大雨",rainy:"有雨",snowy:"下雪","snowy-rainy":"雨夹雪",sunny:"晴",windy:"有风","windy-variant":"多云有风"};
const weatherText = value => {
  const lines = [`${value.location} · ${conditions[value.condition] || value.condition}`];
  if(value.temperature != null) lines.push(`当前温度：${value.temperature}${value.temperature_unit || "°C"}`);
  if(value.humidity != null) lines.push(`湿度：${value.humidity}%`);
  if(value.wind_speed != null) lines.push(`风速：${value.wind_speed} ${value.wind_speed_unit || ""}`);
  if(value.forecast?.length) lines.push("", "天气预报", ...value.forecast.map(f => `${time(f.datetime)} · ${conditions[f.condition] || f.condition || ""} · ${f.templow == null ? "" : f.templow + "～"}${f.temperature ?? "—"}${value.temperature_unit || "°C"}`));
  lines.push("", `更新时间：${time(value.updated_at)}（北京时间）`);
  return lines.join("\n");
};

class HAAIToolsPanel extends HTMLElement {
  constructor() {
    super(); this.attachShadow({mode:"open"}); this._tab="reminders"; this._busy=false; this._message="";
  }
  set hass(value) { this._hass=value; if(!this._loaded) {this._loaded=true; this.load();} }
  connectedCallback() { this.render(); }
  async load() {
    try { this._data=await this._hass.callWS({type:"ha_ai_tools/data"}); this._locations=Object.entries(this._data.settings.locations || {}); this._error=""; }
    catch(error) { this._error=error.message || "无法读取配置，请确认已添加集成且当前账户是管理员。"; }
    this.render();
  }
  async action(action,payload={}) {
    if(this._busy) return;
    this._busy=true; this._message="正在处理…"; this.render();
    try {
      const result=await this._hass.callWS({type:"ha_ai_tools/action",action,payload});
      this._message=action==="test_broadcast" ? Object.values(result).every(v=>v==="delivered") ? "播报已发送，请核对实际声音。" : "部分设备播报失败，请检查设备状态。" : "已保存";
      if(action==="delete_reminder_history") this._message="已删除这条历史记录";
      if(action==="clear_reminder_history") this._message=`已清理 ${result.deleted} 条历史记录，待执行和正在播报的提醒已保留。`;
      if(action==="save_connection") {this._connectionDraft=null;this._message=payload.delete?"已删除模型连接":"连接验证通过，已保存";}
      if(action==="save_profile") {this._profileDraft=null;this._message=payload.delete?"已删除对话助手":"对话助手已保存";}
      if(action==="test_connection") this._message="已保存连接测试通过";
      if(action==="validate_functions") this._message=`函数配置检查通过，共 ${result.count} 个函数${result.names?.length?"："+result.names.join("、"):""}`;
      if(action==="preview_legacy") {this._importPreview={...result,source_entity:payload.source_entity};this._message="已生成导入预览，请核对后确认导入。";}
      if(action==="import_legacy") {this._importPreview=null;}
      if(action==="import_legacy") this._message=[result.already_imported?"该来源已导入，无需重复创建":"已验证并导入独立连接与对话助手",...(result.warnings||[])].join("\n");
      this._edit=null; this._reminderEdit=null; await this.load();
    }
    catch(error) { this._message=error.message || "操作失败，请稍后重试。"; }
    finally {this._busy=false; this.render();}
  }
  render() {
    const d=this._data;
    this.shadowRoot.innerHTML=`<style>
      :host{display:block;height:100%;color:var(--primary-text-color,#172b38);background:var(--primary-background-color,#f3f6f8);font-family:var(--paper-font-body1_-_font-family,system-ui);overflow:auto}
      *{box-sizing:border-box} header{display:flex;align-items:center;gap:16px;padding:22px 28px;background:var(--card-background-color,#fff);border-bottom:1px solid var(--divider-color,#e4e9ed)}
      h1{font-size:22px;margin:0}p{line-height:1.6;color:var(--secondary-text-color,#62747d);margin:8px 0} .grow{flex:1}.subtitle{font-size:13px;margin-top:4px}
      main{max-width:1080px;margin:auto;padding:28px}nav{display:flex;gap:8px;margin-bottom:24px;flex-wrap:wrap}button,input,select,textarea{font:inherit}
      button{border:1px solid var(--divider-color,#d9e1e6);background:var(--card-background-color,#fff);color:inherit;padding:10px 16px;border-radius:12px;cursor:pointer;transition:transform .15s,background .2s,box-shadow .2s}
      button:hover{transform:translateY(-1px);box-shadow:0 3px 12px #0000000d}button:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible{outline:3px solid var(--primary-color,#137f77);outline-offset:2px}
      button.primary,nav button.active{background:var(--primary-color,#137f77);color:var(--text-primary-color,#fff);border-color:transparent}button:disabled{opacity:.5;cursor:wait;transform:none}.danger{color:var(--error-color,#b83939)}
      .grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}.card{background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#e3e9ed);border-radius:20px;padding:24px;margin-bottom:18px;animation:appear .2s ease-out}h2{font-size:18px;margin:0 0 14px}h3{font-size:16px;margin:0}
      label.field{display:block;margin:14px 0;font-weight:500}input:not([type=checkbox]),textarea,select{display:block;width:100%;margin-top:7px;border:1px solid var(--divider-color,#cfd9df);border-radius:10px;padding:11px;background:var(--primary-background-color,#fafcfd);color:inherit}textarea{min-height:92px;resize:vertical}
      .row{display:flex;align-items:center;gap:14px;flex-wrap:wrap}.row>.field{flex:1;min-width:170px}.check{display:flex;align-items:center;gap:14px;border:1px solid var(--divider-color,#e3e9ed);padding:16px;border-radius:14px;margin:10px 0;cursor:pointer}.check input{width:20px;height:20px;accent-color:var(--primary-color,#137f77)}small{color:var(--secondary-text-color,#62747d);display:block;margin-top:5px}
      .badge{font-size:12px;padding:4px 9px;border-radius:20px;background:var(--secondary-background-color,#edf4f3);white-space:nowrap}.badge.failed,.badge.missed{color:var(--error-color,#b83939)}.item{padding:16px 0;border-bottom:1px solid var(--divider-color,#e5ebee)}.item:last-child{border:0}.empty{padding:28px;text-align:center;color:var(--secondary-text-color,#62747d)}.notice{border-left:3px solid var(--primary-color,#137f77);padding:12px 16px;margin:16px 0;background:var(--card-background-color,#fff);border-radius:6px}.hint{font-size:13px}.actions{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}.place{display:grid;grid-template-columns:1fr 1.7fr auto;gap:10px;align-items:end;margin-bottom:15px}.place label{margin:0}.result{white-space:pre-wrap;word-break:break-word;background:var(--primary-background-color);padding:14px;border-radius:10px}details{margin:14px 0}summary{cursor:pointer}.count{font-size:32px;font-weight:650;margin:8px 0}
      @keyframes appear{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
      fieldset{min-width:0;border:0;padding:0;margin:20px 0}legend{font-weight:600;padding:0;margin-bottom:4px}.form-error{color:var(--error-color,#b83939);margin:10px 0}.device-editor{margin-top:14px;padding:14px;border-radius:14px;background:var(--primary-background-color,#fafcfd);animation:appear .2s ease-out}
      .code{font-family:ui-monospace,SFMono-Regular,monospace;font-size:13px;line-height:1.5}.endpoint{overflow-wrap:anywhere}.notice{white-space:pre-wrap}summary{padding:8px 0;font-weight:600}details[open]>:not(summary){animation:appear .2s ease-out}.reminder-toolbar{margin-bottom:14px}.reminder-toolbar h2{margin:0}.history-hint{margin-bottom:8px}
      @media(max-width:700px){header{padding:16px}main{padding:16px}.grid{grid-template-columns:1fr}.card{padding:18px}.place{grid-template-columns:1fr}.subtitle{max-width:210px}nav button{flex:1;padding:10px 8px;font-size:14px}}
    </style><header><ha-menu-button></ha-menu-button><div class="grow"><h1>AI 家庭助手</h1><div class="subtitle">提醒、天气、记忆与模型 · 所有时间均为北京时间</div></div><button id="refresh" aria-label="刷新数据">刷新</button></header><main>
      ${d?.migration_error?`<div class="notice" role="alert">${esc(d.migration_error)}</div>`:""}
      ${this._error?`<div class="notice" role="alert">${esc(this._error)}</div>`:""}
      ${this._message?`<div class="notice" role="status" aria-live="polite">${esc(this._message)}</div>`:""}
      ${!d?`<div class="empty">${this._error?"请检查集成配置后刷新":"正在读取家庭助手…"}</div>`:`
      <nav aria-label="功能"><button data-tab="reminders" class="${this._tab==="reminders"?"active":""}">提醒</button><button data-tab="outputs" class="${this._tab==="outputs"?"active":""}">播报设备</button><button data-tab="weather" class="${this._tab==="weather"?"active":""}">天气地点</button><button data-tab="memory" class="${this._tab==="memory"?"active":""}">永久记忆</button><button data-tab="connections" class="${this._tab==="connections"?"active":""}">模型连接</button><button data-tab="profiles" class="${this._tab==="profiles"?"active":""}">对话助手</button></nav>
      ${this._tab==="reminders"?this.reminders(d):this._tab==="outputs"?this.outputs(d):this._tab==="weather"?this.weather(d):this._tab==="connections"?this.connections(d):this._tab==="profiles"?this.profiles(d):this.memory(d)}`}
    </main>`;
    const root=this.shadowRoot;
    const menu=root.querySelector("ha-menu-button"); if(menu) menu.hass=this._hass;
    root.querySelector("#refresh").onclick=()=>{this.captureNativeForm();this.load();};
    root.querySelectorAll("[data-tab]").forEach(b=>b.onclick=()=>{this.captureNativeForm();this._tab=b.dataset.tab;this._message="";this.render();});
    const historyCount=d?.history_count ?? (d?.reminders||[]).filter(r=>historyStatuses.has(r.status)).length;
    root.querySelectorAll("button").forEach(b=>b.disabled=this._busy || b.hasAttribute("data-locked") || b.id==="clear-history" && !historyCount);
    root.querySelectorAll("[data-cancel]").forEach(b=>b.onclick=()=>this.action("cancel_reminder",{id:b.dataset.cancel}));
    root.querySelectorAll("[data-delete-history]").forEach(b=>b.onclick=()=>{
      const item=d.reminders.find(r=>r.id===b.dataset.deleteHistory);
      if(item && confirm(`确认删除历史记录“${item.message}”？删除后不可恢复。`)) this.action("delete_reminder_history",{id:item.id});
    });
    root.querySelector("#clear-history")?.addEventListener("click",()=>{
      if(historyCount && confirm(`确认清理全部 ${historyCount} 条已结束的提醒记录？待执行和正在播报的提醒会保留。删除后不可恢复。`)) this.action("clear_reminder_history");
    });
    root.querySelector("#reminder-form")?.addEventListener("submit",event=>{
      event.preventDefault(); const f=new FormData(event.target); const payload={message:f.get("message"),at:f.get("at")+":00+08:00",repeat:f.get("repeat")};
      const custom=[...root.querySelectorAll("input[name=reminder-output]:checked")].map(i=>i.value);
      if(!custom.length){root.querySelector("#reminder-error").textContent="请为这条提醒至少选择一个播报设备。";return;}
      payload.outputs=custom;
      this.action("add_reminder",payload);
    });
    root.querySelector("#use-default-outputs")?.addEventListener("click",()=>{root.querySelectorAll("input[name=reminder-output]").forEach(input=>input.checked=(d.settings.outputs||[]).includes(input.value));});
    root.querySelectorAll("[data-edit-reminder-outputs]").forEach(b=>b.onclick=()=>{this._reminderEdit=b.dataset.editReminderOutputs;this.render();});
    root.querySelector("#cancel-reminder-edit")?.addEventListener("click",()=>{this._reminderEdit=null;this.render();});
    root.querySelector("#save-reminder-outputs")?.addEventListener("click",()=>{
      const outputs=[...root.querySelectorAll("input[name=edit-reminder-output]:checked")].map(i=>i.value);
      if(!outputs.length){root.querySelector("#reminder-edit-error").textContent="请至少选择一个播报设备。";return;}
      this.action("update_reminder_outputs",{id:this._reminderEdit,outputs});
    });
    root.querySelector("#save-outputs")?.addEventListener("click",()=>this.action("save_settings",{outputs:[...root.querySelectorAll("input[name=output]:checked")].map(i=>i.value)}));
    root.querySelector("#broadcast")?.addEventListener("click",()=>this.action("test_broadcast"));
    root.querySelector("#add-place")?.addEventListener("click",()=>{this.capturePlaces();this._locations.push(["",d.weather[0]?.entity_id||""]);this.render();});
    root.querySelectorAll("[data-remove-place]").forEach(b=>b.onclick=()=>{this.capturePlaces();this._locations.splice(Number(b.dataset.removePlace),1);this.render();});
    root.querySelector("#save-places")?.addEventListener("click",()=>{
      this.capturePlaces(); const names=this._locations.map(p=>p[0].trim());
      if(names.some(n=>!n)||new Set(names).size!==names.length){this._message="地点名称不能为空，也不能重复。";this.render();return;}
      this.action("save_settings",{locations:Object.fromEntries(this._locations)});
    });
    root.querySelector("#check-weather")?.addEventListener("click",async()=>{
      try{const value=await this._hass.callWS({type:"ha_ai_tools/action",action:"weather",payload:{location:root.querySelector("#weather-place").value,kind:root.querySelector("#weather-kind").value}});root.querySelector("#weather-result").textContent=weatherText(value);}
      catch(error){root.querySelector("#weather-result").textContent=error.message||"天气暂不可用";}
    });
    root.querySelector("#memory-form")?.addEventListener("submit",event=>{event.preventDefault();const f=new FormData(event.target);this.action("remember",{key:f.get("key"),text:f.get("text"),scope:this._edit?.scope||"family"});});
    root.querySelectorAll("[data-edit-memory]").forEach(b=>b.onclick=()=>{this._edit=d.memories.find(m=>m.id===b.dataset.editMemory);this.render();root.querySelector("#memory-form input")?.focus();});
    root.querySelectorAll("[data-forget]").forEach(b=>b.onclick=()=>{if(confirm("确认忘记这条记忆？相关会话缓存也会清除。"))this.action("forget",{id:b.dataset.forget});});
    root.querySelector("#cancel-edit")?.addEventListener("click",()=>{this._edit=null;this.render();});
    if(d) this.bindNativeForms(d);
  }
  captureConnection() {
    const form=this.shadowRoot.querySelector("#connection-form");
    if(!form) return null;
    const f=new FormData(form);
    const payload={name:f.get("name"),api_provider:f.get("api_provider"),base_url:f.get("base_url"),api_key:f.get("api_key"),organization:f.get("organization"),api_version:f.get("api_version"),skip_authentication:f.get("skip_authentication")==="on"};
    if(this._connectionDraft?.id) payload.id=this._connectionDraft.id;
    this._connectionDraft={...this._connectionDraft,...payload,api_key:""};
    return payload;
  }
  captureProfile() {
    const form=this.shadowRoot.querySelector("#profile-form");
    if(!form) return null;
    const f=new FormData(form);
    const payload={name:f.get("name"),connection_id:f.get("connection_id"),chat_model:f.get("chat_model"),prompt:f.get("prompt"),functions:f.get("functions"),context_truncate_strategy:f.get("context_truncate_strategy"),attach_username:f.get("attach_username")==="on",use_tools:f.get("use_tools")==="on"};
    const id=String(f.get("id")||"").trim();
    if(id) payload.id=id;
    for(const key of ["max_tokens","top_p","temperature","max_function_calls_per_conversation","context_threshold"]) payload[key]=Number(f.get(key));
    const previous=this._profileDraft || (this._data?.profiles||[]).find(profile=>profile.id===id);
    if(previous?.functions_redacted && !payload.functions.trim()) delete payload.functions;
    this._profileDraft={...payload,functions_redacted:previous?.functions_redacted && payload.functions===undefined};
    this._functionsOpen=Boolean(this.shadowRoot.querySelector("#profile-functions")?.open);
    this._parametersOpen=Boolean(this.shadowRoot.querySelector("#profile-parameters")?.open);
    return payload;
  }
  captureNativeForm() {
    if(this._tab==="connections") this.captureConnection();
    if(this._tab==="profiles") this.captureProfile();
  }
  bindNativeForms(d) {
    const root=this.shadowRoot;
    root.querySelector("#connection-form")?.addEventListener("submit",event=>{
      event.preventDefault();
      const payload=this.captureConnection();
      if(payload) this.action("save_connection",payload);
    });
    root.querySelectorAll("[data-edit-connection]").forEach(button=>button.onclick=()=>{
      const connection=(d.connections||[]).find(item=>item.id===button.dataset.editConnection);
      if(connection){this._connectionDraft={...connection};this.render();root.querySelector("#connection-form input")?.focus();}
    });
    root.querySelector("#new-connection")?.addEventListener("click",()=>{this._connectionDraft=null;this.render();root.querySelector("#connection-form input")?.focus();});
    root.querySelectorAll("[data-delete-connection]").forEach(button=>button.onclick=()=>{
      const id=button.dataset.deleteConnection;
      if((d.profiles||[]).some(profile=>profile.connection_id===id)) return;
      const connection=(d.connections||[]).find(item=>item.id===id);
      if(connection && confirm(`确认删除模型连接“${connection.name}”？`)){this.captureConnection();this.action("save_connection",{id,delete:true});}
    });
    root.querySelectorAll("[data-test-connection]").forEach(button=>button.onclick=()=>{this.captureNativeForm();this.action("test_connection",{connection_id:button.dataset.testConnection});});
    root.querySelector("#import-legacy")?.addEventListener("submit",event=>{
      event.preventDefault();
      const source_entity=new FormData(event.target).get("source_entity");
      this.captureConnection();
      if(source_entity) this.action("preview_legacy",{source_entity});
    });
    root.querySelector("#confirm-import")?.addEventListener("click",()=>this.action("import_legacy",{source_entity:this._importPreview.source_entity}));
    root.querySelector("#profile-form")?.addEventListener("submit",event=>{
      event.preventDefault();
      const payload=this.captureProfile();
      if(payload) this.action("save_profile",payload);
    });
    root.querySelectorAll("[data-edit-profile]").forEach(button=>button.onclick=()=>{
      const profile=(d.profiles||[]).find(item=>item.id===button.dataset.editProfile);
      if(profile){this._profileDraft={...profile};this.render();root.querySelector("#profile-form input")?.focus();}
    });
    root.querySelector("#new-profile")?.addEventListener("click",()=>{this._profileDraft={...profileDefaults,connection_id:d.connections?.[0]?.id||""};this.render();root.querySelector("#profile-form input")?.focus();});
    root.querySelectorAll("[data-delete-profile]").forEach(button=>button.onclick=()=>{
      const id=button.dataset.deleteProfile;
      if(id==="default") return;
      const profile=(d.profiles||[]).find(item=>item.id===id);
      if(profile && confirm(`确认删除对话助手“${profile.name}”？`)){this.captureProfile();this.action("save_profile",{id,delete:true});}
    });
    root.querySelectorAll("[data-test-profile]").forEach(button=>button.onclick=()=>{this.captureProfile();this.action("test_connection",{profile_id:button.dataset.testProfile});});
    root.querySelector("#validate-functions")?.addEventListener("click",()=>{
      const payload=this.captureProfile();
      if(payload) this.action("validate_functions",payload.functions===undefined?{profile_id:payload.id}:{functions:payload.functions});
    });
  }
  connections(d) {
    const connections=d.connections||[], profiles=d.profiles||[], sources=d.sources||[];
    const edit=this._connectionDraft||{};
    return `<div class="grid"><section class="card"><h2>${edit.id?"修改模型连接":"添加模型连接"}</h2><p>连接和密钥由 AI 工具独立管理。支持 OpenAI、DeepSeek、本地兼容服务及 Azure。</p><p class="hint">${d.native_connection?"已配置独立模型连接":"尚未配置独立模型连接，请先添加或导入。"}</p><form id="connection-form" autocomplete="off">
      <label class="field">连接名称<input name="name" maxlength="100" required value="${esc(edit.name||"")}" placeholder="例如：家庭本地模型"></label>
      <label class="field">接口类型<select name="api_provider"><option value="openai" ${edit.api_provider!=="azure"?"selected":""}>OpenAI 兼容接口</option><option value="azure" ${edit.api_provider==="azure"?"selected":""}>Azure OpenAI</option></select></label>
      <label class="field">API 地址<input type="url" name="base_url" required value="${esc(edit.base_url??"https://api.openai.com/v1")}" placeholder="https://api.example.com/v1"></label>
      <label class="field">API 密钥<input type="password" name="api_key" autocomplete="new-password" value="" placeholder="${edit.api_key_configured?"已配置，留空保留原密钥":"输入密钥；本地免认证服务可启用下方开关"}"><small>密钥不会回显。页面刷新或操作后需重新输入尚未保存的密钥。</small></label>
      <div class="row"><label class="field">组织 · 可选<input name="organization" value="${esc(edit.organization||"")}" placeholder="Organization"></label><label class="field">API 版本 · Azure 必填<input name="api_version" value="${esc(edit.api_version||"")}" placeholder="例如：2024-10-21"></label></div>
      <label class="check"><input type="checkbox" name="skip_authentication" ${edit.skip_authentication?"checked":""}><span>跳过认证<small>仅用于明确无需密钥的兼容服务。</small></span></label>
      ${edit.id?`<small>连接 ID：${esc(edit.id)}</small>`:""}<div class="actions"><button class="primary" type="submit">验证并保存连接</button>${edit.id?`<button type="button" id="new-connection">添加另一连接</button>`:""}</div></form>
      <p class="hint">保存时先验证新连接，失败会保留旧配置。修改已保存连接会影响引用它的全部助手。</p></section>
      <section class="card"><h2>已保存连接 <span class="badge">${connections.length} 组</span></h2>${!connections.length?`<div class="empty">还没有独立连接。添加连接后，在“对话助手”中选择连接和模型。</div>`:connections.map(connection=>{
        const used=profiles.filter(profile=>profile.connection_id===connection.id);
        return `<article class="item"><div class="row"><h3 class="grow">${esc(connection.name)}</h3><span class="badge">${connection.api_provider==="azure"?"Azure":"OpenAI 兼容"}</span></div><p class="endpoint">${esc(connection.base_url)}</p><small>${connection.skip_authentication?"免认证连接":connection.api_key_configured?"已配置密钥":"未配置密钥"} · ${used.length} 个助手引用</small>${used.length?`<small>助手：${used.map(profile=>esc(profile.name)).join("、")}</small>`:""}<div class="actions"><button data-edit-connection="${esc(connection.id)}">修改</button><button data-test-connection="${esc(connection.id)}">测试已保存连接</button><button class="danger" data-delete-connection="${esc(connection.id)}" ${used.length?'disabled data-locked title="请先修改引用此连接的助手"':""}>删除</button></div></article>`;
      }).join("")}</section></div>
      <section class="card"><h2>从旧对话来源导入</h2><p>导入已有连接、助手参数和高级函数。验证成功后启用独立连接；已有提醒、播报设备、天气地点与记忆继续保留。</p><form id="import-legacy"><label class="field">旧 Extended OpenAI 对话来源<select name="source_entity" required ${sources.length?"":"disabled"}><option value="">请选择来源</option>${sources.map(source=>`<option value="${esc(source.entity_id)}" ${source.entity_id===d.settings.source_entity?"selected":""}>${esc(source.name)} · ${esc(source.entity_id)}</option>`).join("")}</select></label><button type="submit" ${sources.length?"":"disabled data-locked"}>预览导入配置</button></form>${this._importPreview?`<div class="notice"><strong>${esc(this._importPreview.name)}</strong>${this._importPreview.profiles.map(p=>`<p>${esc(p.name)} · ${esc(p.chat_model)} · 输出上限 ${esc(p.max_tokens)} · ${esc(p.function_count)} 个函数</p>`).join("")}<p>${(this._importPreview.warnings||[]).map(esc).join("<br>")}</p>${this._importPreview.references?.length?`<p>需要迁移的自动化：${this._importPreview.references.map(esc).join("、")}</p>`:""}<button id="confirm-import" class="primary">确认导入</button></div>`:""}${!sources.length?`<p class="hint">当前未发现可导入的旧对话来源，可直接添加独立连接。</p>`:""}</section>`;
  }
  profiles(d) {
    const profiles=d.profiles||[], connections=d.connections||[];
    const edit={...profileDefaults,...(this._profileDraft||profiles.find(profile=>profile.id==="default")||{})};
    const existing=Boolean(edit.id && profiles.some(profile=>profile.id===edit.id));
    const functions=typeof edit.functions==="string"?edit.functions:JSON.stringify(edit.functions??[],null,2);
    const numberField=(name,label,min,max,step="1")=>`<label class="field">${label}<input type="number" name="${name}" min="${min}" max="${max}" step="${step}" required value="${esc(edit[name])}"></label>`;
    return `<div class="grid"><section class="card"><h2>${existing?"修改对话助手":"添加对话助手"}</h2><p>每个助手独立选择连接、模型和提示词。默认助手继续沿用原对话实体。</p><form id="profile-form">
      <div class="row"><label class="field">助手名称<input name="name" maxlength="100" required value="${esc(edit.name)}"></label><label class="field">助手 ID<input name="id" pattern="[A-Za-z0-9_-]{1,128}" maxlength="128" value="${esc(edit.id||"")}" ${existing?"readonly":""} placeholder="留空自动生成"><small>${existing?"保存后保持稳定":"可用英文字母、数字、下划线和短横线"}</small></label></div>
      <label class="field">模型连接<select name="connection_id" required><option value="">请选择模型连接</option>${connections.map(connection=>`<option value="${esc(connection.id)}" ${connection.id===edit.connection_id?"selected":""}>${esc(connection.name)}</option>`).join("")}</select></label>
      <label class="field">模型名称<input name="chat_model" required value="${esc(edit.chat_model)}" placeholder="例如：deepseek-chat"></label>
      <label class="field">提示词 · 支持 HA 模板<textarea name="prompt" rows="6" required>${esc(edit.prompt)}</textarea><small>可使用 ha_name、exposed_entities、current_device_id 等上下文。</small></label>
      <details id="profile-functions" ${this._functionsOpen?"open":""}><summary>高级函数 · YAML</summary><p class="hint">支持 native、script、template、rest、scrape、composite、sqlite。高级工具访问范围由管理员配置。</p>${edit.functions_redacted?`<p class="hint">原函数包含认证信息，不回显。留空保留原配置；修改时请重新提交完整配置。</p>`:""}<label class="field">函数配置<textarea class="code" name="functions" rows="10" spellcheck="false">${esc(functions)}</textarea></label><button type="button" id="validate-functions">检查函数配置</button><p class="hint">保留原 YAML 格式；保存前也会检查结构、模板与函数名冲突。</p></details>
      <details id="profile-parameters" ${this._parametersOpen?"open":""}><summary>高级对话参数</summary><div class="row">${numberField("max_tokens","最大回答 token 数",1,1000000)}${numberField("max_function_calls_per_conversation","每轮最多函数调用",0,1000000)}</div><div class="row">${numberField("temperature","随机性 · temperature",0,2,"0.01")}${numberField("top_p","采样范围 · top_p",0,1,"0.01")}</div><div class="row">${numberField("context_threshold","上下文 token 阈值",1,10000000)}<label class="field">上下文超限策略<select name="context_truncate_strategy"><option value="clear" selected>clear · 清理当前助手会话上下文</option></select></label></div><label class="check"><input type="checkbox" name="attach_username" ${edit.attach_username?"checked":""}><span>附加已确认用户名称</span></label><label class="check"><input type="checkbox" name="use_tools" ${edit.use_tools?"checked":""}><span>使用 tools 调用协议<small>关闭后使用兼容旧版本的 functions 协议。</small></span></label></details>
      <div class="actions"><button class="primary" type="submit" ${connections.length?"":"disabled data-locked"}>保存助手</button><button type="button" id="new-profile" ${connections.length?"":"disabled data-locked"}>新增助手</button></div>${!connections.length?`<p class="hint">请先在“模型连接”中保存至少一组连接。</p>`:""}</form></section>
      <section class="card"><h2>对话助手 <span class="badge">${profiles.length} 个</span></h2>${profiles.map(profile=>`<article class="item"><div class="row"><h3 class="grow">${esc(profile.name)}</h3>${profile.id==="default"?`<span class="badge">默认助手</span>`:""}</div><p>${esc(profile.chat_model)}</p><small>连接：${esc(connections.find(connection=>connection.id===profile.connection_id)?.name||"未选择连接")}</small><small>助手 ID：${esc(profile.id)}</small><div class="actions"><button data-edit-profile="${esc(profile.id)}">修改</button><button data-test-profile="${esc(profile.id)}" ${profile.connection_id?"":"disabled data-locked"}>测试已保存连接</button>${profile.id!=="default"?`<button class="danger" data-delete-profile="${esc(profile.id)}">删除</button>`:""}</div></article>`).join("")}</section></div>`;
  }
  reminders(d) {
    const pending=d.reminders.filter(r=>r.status==="pending").length;
    const date=new Intl.DateTimeFormat("sv-SE",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(Date.now()+3600000)).replace(" ","T");
    return `<div class="grid"><section class="card"><h2>创建提醒</h2><p>每条提醒可以选择不同的设备，也可以选择多台同时播报。</p><form id="reminder-form"><label class="field">提醒内容<textarea name="message" maxlength="1000" required placeholder="例如：记得给花浇水"></textarea></label><div class="row"><label class="field">提醒时间 · 北京时间<input type="datetime-local" name="at" required value="${esc(date)}"></label><label class="field">重复<select name="repeat">${Object.entries(repeats).map(([v,t])=>`<option value="${v}">${t}</option>`).join("")}</select></label></div><fieldset><legend>本条提醒的播报设备</legend><p class="hint">已按默认设置预选，可以为这条提醒单独调整。修改此处不影响其他提醒。</p>${this.speakerChoices(d,"reminder-output",d.settings.outputs||[])}<button type="button" id="use-default-outputs">恢复默认设备</button></fieldset><div id="reminder-error" class="form-error" role="alert"></div><button class="primary" type="submit">保存提醒</button></form><p class="hint">语音也可以说：“明天九点提醒我喝水，只在 Nabu 播报”。</p></section><section class="card"><div class="row reminder-toolbar"><h2 class="grow">提醒记录 <span class="badge">${pending} 项待处理</span></h2><button class="danger" id="clear-history">清理历史</button></div><p class="hint history-hint">可删除已完成、已取消、失败或错过的记录。清理历史不会影响待执行的提醒。</p>${!d.reminders.length?`<div class="empty">还没有提醒。创建后，设备会在设定时间播报。</div>`:d.reminders.map(r=>`<article class="item"><div class="row"><h3 class="grow">${esc(r.message)}</h3><span class="badge ${esc(r.status)}">${esc(labels[r.status]||r.status)}</span></div><small>${time(r.at)} · ${esc(repeats[r.repeat]||r.repeat)}</small><small>播报设备：${(r.outputs||[]).map(id=>esc(d.outputs.find(o=>o.entity_id===id)?.name||id)).join("、")}</small>${r.delivery?`<small>上次投递：${Object.values(r.delivery).filter(v=>v==="delivered").length}/${Object.keys(r.delivery).length} 台成功</small>`:""}${historyStatuses.has(r.status)?`<div class="actions"><button class="danger" data-delete-history="${esc(r.id)}">删除记录</button></div>`:""}${r.status==="pending"?`<div class="actions"><button data-edit-reminder-outputs="${esc(r.id)}">修改播报设备</button><button class="danger" data-cancel="${esc(r.id)}">取消提醒</button></div>${this._reminderEdit===r.id?`<div class="device-editor"><fieldset><legend>修改这条提醒的播报设备</legend>${this.speakerChoices(d,"edit-reminder-output",r.outputs||[])}</fieldset><div id="reminder-edit-error" class="form-error" role="alert"></div><div class="actions"><button class="primary" id="save-reminder-outputs">保存设备</button><button id="cancel-reminder-edit">取消修改</button></div></div>`:""}`:""}</article>`).join("")}</section></div>`;
  }
  speakerChoices(d,name,selected) {
    return !d.outputs.length?`<div class="empty">未发现支持播报的设备。</div>`:d.outputs.map(o=>{
      const aliases=(o.aliases||[]).filter(alias=>alias && !/^\d+$/.test(alias));
      return `<label class="check"><input type="checkbox" name="${name}" value="${esc(o.entity_id)}" ${selected.includes(o.entity_id)?"checked":""}><span class="grow"><strong>${esc(o.name)}</strong><small>${esc(o.area)}${aliases.length?` · ${aliases.map(esc).join("、")}`:""}</small></span><span class="badge">${o.available?"可用":"离线"}</span></label>`;
    }).join("");
  }
  outputs(d) {
    return `<section class="card"><h2>选择默认播报设备</h2><p>创建提醒时会预选这些设备，语音未指定设备时也使用此设置。每条提醒都可以在“提醒”页面独立选择设备；已有提醒保留原选择。某台设备失败不影响其他设备。</p>${this.speakerChoices(d,"output",d.settings.outputs||[])}<div class="actions"><button class="primary" id="save-outputs">保存选择</button><button id="broadcast">测试已保存设备的播报</button></div><p class="hint">默认对话助手：${esc((d.profiles||[]).find(profile=>profile.id==="default")?.name||"未配置")} · 模型：${esc(d.native_connection?(d.profiles||[]).find(profile=>profile.id==="default")?.chat_model||"未选择":"尚未配置独立连接")}。测试按钮会实际发声。</p></section>`;
  }
  capturePlaces() { this._locations=[...this.shadowRoot.querySelectorAll(".place")].map(row=>[row.querySelector("input").value,row.querySelector("select").value]); }
  weather(d) {
    const locations=this._locations||[];
    return `<section class="card"><h2>天气地点</h2><p>为已配置的天气实体取一个自然名称，例如“家”“公司”。第一个地点为默认查询地点。</p>${locations.map(([name,entity],i)=>`<div class="place"><label class="field">地点名称<input maxlength="80" value="${esc(name)}" placeholder="例如：家"></label><label class="field">天气来源<select>${d.weather.map(w=>`<option value="${esc(w.entity_id)}" ${w.entity_id===entity?"selected":""}>${esc(w.name)} · ${esc(w.entity_id)}</option>`).join("")}</select></label><button data-remove-place="${i}" class="danger" aria-label="移除地点 ${esc(name)}">移除</button></div>`).join("")}<div class="actions"><button id="add-place">添加地点</button><button class="primary" id="save-places">保存地点</button></div></section><section class="card"><h2>验证天气查询</h2><div class="row"><label class="field">已保存地点<select id="weather-place">${Object.keys(d.settings.locations||{}).map(n=>`<option>${esc(n)}</option>`).join("")}</select></label><label class="field">查询内容<select id="weather-kind"><option value="current">当前天气</option><option value="daily">每日预报</option><option value="hourly">每小时预报</option></select></label></div><button id="check-weather">查询真实天气</button><pre id="weather-result" class="result" role="status">查询结果显示在这里</pre></section>`;
  }
  memory(d) {
    const edit=this._edit;
    return `<div class="grid"><section class="card"><h2>${edit?"修改记忆":"添加家庭记忆"}</h2><p>语音会自动保存明确、稳定的偏好。也可以说“记住我喜欢26度”“忘记我的温度偏好”。密码和密钥不会保存。</p><form id="memory-form"><label class="field">类别<input name="key" maxlength="80" required placeholder="例如：空调温度" value="${esc(edit?.key||"")}" ${edit?"readonly":""}></label><label class="field">内容<textarea name="text" maxlength="800" required placeholder="例如：空调通常设置为26度">${esc(edit?.text||"")}</textarea></label><div class="actions"><button class="primary" type="submit">${edit?"更新记忆":"保存记忆"}</button>${edit?`<button type="button" id="cancel-edit">取消修改</button>`:""}</div></form><p class="hint">记忆在 HA 本地持久保存，每次只将相关内容提供给当前配置的模型。当前有 ${d.session_count} 个设备/会话的近期上下文。</p></section><section class="card"><h2>永久记忆 <span class="badge">${d.memories.length} 条</span></h2>${!d.memories.length?`<div class="empty">还没有永久记忆。试着说：“记住我喜欢简短回答”。</div>`:d.memories.map(m=>`<article class="item"><h3>${esc(m.key)}</h3><p>${esc(m.text)}</p><small>${m.scope==="family"?"家庭共享":"已确认用户的个人记忆"} · ${time(m.updated_at)}</small><div class="actions"><button data-edit-memory="${esc(m.id)}">修改</button><button class="danger" data-forget="${esc(m.id)}">忘记</button></div></article>`).join("")}</section></div>`;
  }
}
if(!customElements.get("ha-ai-tools-panel")) customElements.define("ha-ai-tools-panel",HAAIToolsPanel);
