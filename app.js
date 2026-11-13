const MAX_SIZE=250*1024*1024, $=s=>document.querySelector(s);
const state={
  docs:[],
  tags:[],
  selected:[],
  file:null,
  filter:[],
  metaFilter:{},
  masters:{},
  formMeta:{documentType:'',financialYear:'',month:'',itemText:'',vendorText:''},
  databaseBackend:'sqlite',
  fileBackend:'local',
  smbMounted:false,
  networkReachable:null,
  currentUser:null,
  authMode:'login',
  setupRequired:false
};

const CLASS_FIELDS={
  po_id:['PO ID'],
  financialYear:['Financial Year'],
  month:['Month'],
  item_code:['Item Code'],
  itemText:['Item Name'],
  vendor_code:['Vendor Code'],
  vendorText:['Vendor Name'],
  documentType:['Document Type']
};
const MANAGED_FIELDS={
  documentType:CLASS_FIELDS.documentType,
  financialYear:CLASS_FIELDS.financialYear
};
const MONTHS=['January','February','March','April','May','June','July','August','September','October','November','December'];
const PO_DOCUMENT_TYPES=[
  "Purchase Order","Quotation","Financial Approval","Technical Specification",
  "Comparative Statement","Work Order","GRN (Goods Received Note)","Invoice",
  "Delivery Challan","Inspection Report","Other"
];

const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const sizeText=n=>n<1024*1024?`${(n/1024).toFixed(0)} KB`:`${(n/1024/1024).toFixed(2)} MB`;
const extOf=name=>name.split('.').pop().toLowerCase();
const escapeAttr=s=>esc(s).replace(/`/g,'&#96;');

async function api(url,opts){
  const r=await fetch(url,opts);
  let data={};
  try{data=await r.json()}catch{}
  if(!r.ok)throw Error(data.error||`Request failed (${r.status})`);
  return data;
}

function friendlyError(e){
  if(!(e instanceof TypeError||/Failed to fetch|NetworkError|Load failed/i.test(e.message)))return e.message;
  if(location.protocol==='file:')return 'This page was opened directly as a file. Start the app with run.command, then open the http://127.0.0.1 address printed in Terminal. Do not open index.html directly.';
  return `Could not reach the local app at ${location.origin}. Make sure the server window running the app is still open.`;
}

function showConnectionError(message){
  const text=$('#connectionText'), notice=$('#connectionNotice');
  if(text)text.textContent=message;
  if(notice)notice.hidden=false;
}

function clientLogs(){
  try{return JSON.parse(localStorage.getItem('efile-client-logs')||'[]')}catch{return []}
}

async function recordFailure(action,message){
  const entry={timestamp:new Date().toISOString(),level:'ERROR',action,message};
  try{
    const entries=[entry,...clientLogs()].slice(0,100);
    localStorage.setItem('efile-client-logs',JSON.stringify(entries));
  }catch{}
  try{
    await fetch('/api/logs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,message})});
  }catch{}
}

function updateNetworkStatus(){
  const el=$('#networkStatus');
  if(!el)return;
  let full,short,status;
  if(state.databaseBackend==='postgres'){
    status=state.networkReachable?'connected':'disconnected';
    full=state.networkReachable?'PostgreSQL connected':'PostgreSQL disconnected';
    short=state.networkReachable?'PG online':'PG offline';
  }else if(state.fileBackend==='smb'){
    const connected=!!state.smbMounted;
    status=connected?'connected':'disconnected';
    full=connected?'SMB share connected':'SMB share disconnected';
    short=connected?'SMB online':'SMB offline';
  }else{
    status=state.networkReachable===false?'disconnected':'local';
    full=state.networkReachable===false?'App unavailable':'Local storage';
    short=state.networkReachable===false?'App offline':'Local storage';
  }
  el.dataset.state=status;
  el.setAttribute('aria-label',full);
  const lbl=$('#networkStatusLabel');
  if(lbl)lbl.textContent=window.matchMedia('(max-width:520px)').matches?short:full;
}

async function refresh(){
  try{
    const cfg=await api('/api/storage/settings');
    state.databaseBackend=cfg.database.backend;
    state.fileBackend=cfg.files.backend;
    state.smbMounted=!!cfg.smbMounted;
  }catch{}
  try{
    const [d,s]=await Promise.all([api('/api/documents'),api('/api/storage')]);
    state.networkReachable=true;
    state.docs=d.documents||[];
    state.tags=d.tags||[];
    const storageUsed=$('#storageUsed'), storageCount=$('#storageCount');
    if(storageUsed)storageUsed.textContent=formatStorage(s.usedBytes||0);
    if(storageCount)storageCount.textContent=`${s.documents||0} document${s.documents===1?'':'s'} · ${s.backend==='smb'?'SMB share':'local storage'}`;
    const notice=$('#connectionNotice');
    if(notice)notice.hidden=true;
    updateNetworkStatus();
    render();
  }catch(e){
    state.networkReachable=false;
    updateNetworkStatus();
    showConnectionError(friendlyError(e));
  }
}

function formatStorage(bytes){
  return bytes>=1_000_000_000?`${(bytes/1_000_000_000).toFixed(2)} GB`:`${(bytes/1_000_000).toFixed(2)} MB`;
}

function localTime(value){
  const d=new Date(value);
  return Number.isNaN(d.getTime())?value:d.toLocaleString(undefined,{year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'});
}

async function getAllLogs(){
  let serverLogs=[];
  try{serverLogs=(await api('/api/logs')).logs||[]}catch{}
  const merged=[...serverLogs,...clientLogs()];
  const unique=[...new Map(merged.map(x=>[`${x.timestamp}|${x.action}|${x.message}`,x])).values()];
  return unique.sort((a,b)=>new Date(b.timestamp)-new Date(a.timestamp)).slice(0,200);
}

async function loadLogs(){
  const box=$('#logsList');
  if(!box)return;
  box.innerHTML='<div class="logs-empty">Loading logs…</div>';
  const logs=await getAllLogs();
  box.innerHTML=logs.length?logs.map(x=>`<div class="log-entry"><span class="log-time">${esc(localTime(x.timestamp))}</span><span class="log-level">${esc(x.level)}</span><span class="log-action">${esc(x.action)}</span><span class="log-message">${esc(x.message)}</span></div>`).join(''):'<div class="logs-empty">No failures have been recorded.</div>';
}

function getDocSearchTokens(d){
  const tokens=[
    d.po_id,
    d.financialYear,
    d.month,
    d.itemText,
    d.item_code,
    d.vendor_code,
    d.vendorText,
    d.name,
    d.documentType,
    d.serialKey,
    d.remark,
    ...(d.tags||[])
  ];

  if(d.month){
    const mStr=String(d.month).trim();
    if(!isNaN(mStr)&&+mStr>=1&&+mStr<=12){
      tokens.push(MONTHS[+mStr-1]);
    }else{
      const mIdx=MONTHS.findIndex(m=>m.toLowerCase()===mStr.toLowerCase());
      if(mIdx!==-1){
        tokens.push(String(mIdx+1), String(mIdx+1).padStart(2,'0'));
      }
    }
  }

  if(d.uploadedAt){
    tokens.push(d.uploadedAt);
    try{
      const dt=new Date(d.uploadedAt);
      if(!isNaN(dt.getTime())){
        tokens.push(
          dt.toISOString().slice(0,10),
          localTime(d.uploadedAt),
          dt.toLocaleDateString(),
          dt.getFullYear().toString(),
          (dt.getMonth()+1).toString(),
          String(dt.getMonth()+1).padStart(2,'0'),
          dt.getDate().toString(),
          String(dt.getDate()).padStart(2,'0'),
          dt.toLocaleString(undefined,{month:'short'}),
          dt.toLocaleString(undefined,{month:'long'})
        );
      }
    }catch{}
  }

  return tokens.filter(Boolean).map(v=>String(v).toLowerCase());
}

function parsedQuery(){
  const el=$('#search');
  if(!el) return [];
  const val=el.value.trim();
  if(!val) return [];
  if(val.includes(',')){
    return val.split(',').map(x=>x.trim()).filter(Boolean).slice(0,20);
  }
  return val.split(/\s+/).filter(Boolean).slice(0,20);
}

function renderFilters(){
  const filterBox=$('#fieldFilters');
  if(!filterBox)return;

  const poInput=$('#searchPoId');
  const fySelect=$('#searchFinYear');

  const poOptions=[...new Set(state.docs.map(d=>d.po_id).filter(Boolean))].sort();
  const fyOptions=[...new Set([...(state.masters.financialYear||[]), ...state.docs.map(d=>d.financialYear).filter(Boolean)])].sort().reverse();

  if(!poInput || !fySelect){
    filterBox.innerHTML=`
      <label for="searchPoId">Purchase Order (PO ID)
        <input type="text" id="searchPoId" data-meta-filter="po_id" placeholder="Enter or search PO ID (e.g. 0010, MMD/2026-27/0010)…" list="poSearchDatalist" value="${escapeAttr(state.metaFilter.po_id||'')}" autocomplete="off">
        <datalist id="poSearchDatalist">
          ${poOptions.map(p=>`<option value="${escapeAttr(p)}">`).join('')}
        </datalist>
      </label>
      <label for="searchFinYear">Financial Year
        <select id="searchFinYear" data-meta-filter="financialYear">
          <option value="">All Financial Years</option>
          ${fyOptions.map(f=>`<option value="${escapeAttr(f)}" ${state.metaFilter.financialYear===f?'selected':''}>${esc(f)}</option>`).join('')}
        </select>
      </label>
    `;
  } else {
    const dl=$('#poSearchDatalist');
    if(dl){
      dl.innerHTML=poOptions.map(p=>`<option value="${escapeAttr(p)}">`).join('');
    }
    if(document.activeElement!==poInput){
      poInput.value=state.metaFilter.po_id||'';
    }
    if(document.activeElement!==fySelect){
      fySelect.value=state.metaFilter.financialYear||'';
    }
  }
}

function renderDocumentList(){
  const docCount=$('#docCount');
  if(docCount)docCount.textContent=state.docs.length;

  const targetPo=(state.metaFilter.po_id||'').trim().toLowerCase();
  const targetFy=(state.metaFilter.financialYear||'').trim().toLowerCase();

  const filtered=state.docs.filter(d=>{
    // PO ID filter (case-insensitive substring)
    if(targetPo){
      const docPo=String(d.po_id||'').toLowerCase();
      if(!docPo.includes(targetPo))return false;
    }
    // Financial Year filter (exact or substring)
    if(targetFy){
      const docFy=String(d.financialYear||'').toLowerCase();
      if(docFy!==targetFy && !docFy.includes(targetFy))return false;
    }
    return true;
  });

  const n=(targetPo?1:0)+(targetFy?1:0);
  const hint=$('#filterHint');
  if(hint){
    if(n===0){
      hint.textContent=`Showing all ${state.docs.length} documents`;
    }else{
      hint.textContent=`${filtered.length} matching document${filtered.length===1?'':'s'} found (${n} active search filter${n===1?'':'s'})`;
    }
  }

  const expBtn=$('#exportExcel');
  if(expBtn)expBtn.hidden=state.currentUser?.role!=='admin';

  const list=$('#documentList'),empty=$('#emptyState');
  if(!list||!empty)return;
  if(!filtered.length){
    list.innerHTML='';
    empty.hidden=false;
    $('#emptyTitle').textContent=state.docs.length?'No matching documents found.':'No documents yet';
    $('#emptyText').textContent=state.docs.length?'Try adjusting your PO ID or Financial Year search.':'Upload a document to start your library.';
    return;
  }
  empty.hidden=true;
  const isAdmin=state.currentUser?.role==='admin';
  list.innerHTML=filtered.map(d=>{
    const x=extOf(d.name),type=['png','jpg','jpeg','gif','webp','bmp','tif','tiff'].includes(x)?'img':x;
    return `<article class="doc-row">
      <div class="file-icon ${type}">${esc(x.toUpperCase().slice(0,4))}</div>
      <div class="doc-main">
        <div class="doc-serial-row" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px;">
          <span class="badge-po">PO: ${esc(d.po_id||'N/A')}</span>
          ${d.financialYear?`<span class="badge-fy">${esc(d.financialYear)}</span>`:''}
          <span class="doc-serial">${esc(d.serialKey||'')}</span>
        </div>
        <div class="doc-name" title="${escapeAttr(d.name)}">${esc(d.name)}</div>
        <div class="doc-sub">${sizeText(d.size)} · ${esc(localTime(d.uploadedAt))}${d.documentType?` · <b>${esc(d.documentType)}</b>`:''}</div>
        <div class="doc-classification">
          ${(d.item_code||d.itemText)?`<span><b>Item:</b> ${esc(d.item_code?`[${d.item_code}] `:'' )}${esc(d.itemText||'')}</span>`:''}
          ${(d.vendor_code||d.vendorText)?`<span><b>Vendor:</b> ${esc(d.vendor_code?`[${d.vendor_code}] `:'' )}${esc(d.vendorText||'')}</span>`:''}
          ${d.month?`<span><b>Month:</b> ${esc(d.month)}</span>`:''}
          ${d.remark?`<span><b>Remark:</b> ${esc(d.remark)}</span>`:''}
        </div>
      </div>
      <div class="row-actions">
        <button class="action view" data-view="${escapeAttr(d.id)}">Preview</button>
        <a class="action" href="/api/file/${encodeURIComponent(d.id)}" target="_blank" rel="noopener">Open ↗</a>
        ${isAdmin?`<button class="action" data-edit="${escapeAttr(d.id)}">Edit</button><button class="action danger-action" data-delete="${escapeAttr(d.id)}">Delete</button>`:''}
      </div>
    </article>`;
  }).join('');
}

function render(){
  renderFilters();
  renderDocumentList();
}

function toast(msg){
  const t=$('#toast');
  if(!t)return;
  t.textContent=msg;
  t.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer=setTimeout(()=>t.classList.remove('show'),2800);
}

function fileKind(name){
  const x=extOf(name);
  return ['png','jpg','jpeg','gif','webp','bmp','tif','tiff'].includes(x)?'image':x==='pdf'?'pdf':'word';
}

function openView(d){
  $('#viewTitle').textContent=d.name;
  const fields=[
    ['PO ID', d.po_id],
    ['Financial Year', d.financialYear],
    ['Month', d.month],
    ['Item Code', d.item_code],
    ['Item Name', d.itemText],
    ['Vendor Code', d.vendor_code],
    ['Vendor Name', d.vendorText],
    ['Document Type', d.documentType],
    ['Remarks', d.remark],
    ['Unique Key', d.serialKey],
    ['Document Name', d.name]
  ].filter(([lbl,val]) => val && String(val).trim());

  $('#viewTags').innerHTML=[
    ...fields.map(([k,v])=>`<span class="doc-tag"><b>${esc(k)}:</b> ${esc(v)}</span>`),
    `<span class="doc-tag"><b>Uploaded:</b> ${esc(localTime(d.uploadedAt))}</span>`
  ].join('');
  $('#openAttachment').href=`/api/file/${encodeURIComponent(d.id)}`;
  const body=$('#viewBody');
  body.replaceChildren();
  const kind=fileKind(d.name),url=`/api/file/${encodeURIComponent(d.id)}`;
  if(kind==='image'){
    const img=document.createElement('img');
    img.src=url;
    img.alt=d.name;
    img.onerror=()=>{
      body.innerHTML='<div class="view-fallback error"><b>Document file not accessible</b><p>The file associated with this record could not be loaded from disk.</p></div>';
    };
    body.append(img);
  }else if(kind==='pdf'){
    const frame=document.createElement('iframe');
    frame.src=url;
    frame.title=d.name;
    body.append(frame);
  }else{
    body.innerHTML='<div class="view-fallback"><b>Preview unavailable for this format</b><p>Use the "Open Attachment" button above to view or download this document.</p></div>';
  }
  $('#viewDialog').showModal();
}

let currentPage='home',previousPage='home';
function showPage(page){
  if(page!=='upload'&&currentPage!=='upload')previousPage=page;
  if(page==='upload'&&currentPage!=='upload')previousPage=currentPage;
  currentPage=page;
  document.querySelectorAll('.page').forEach(p=>{
    p.hidden=p.id!==`${page}Page`;
    p.classList.toggle('active',!p.hidden);
  });
  document.querySelectorAll('.nav-item').forEach(b=>b.classList.toggle('active',b.dataset.pageLink===page));
  const pageTitle=$('#pageTitle');
  if(pageTitle)pageTitle.textContent={home:'Home',upload:'Upload',view:'View Files'}[page]||'Home';
  const upBack=$('#uploadBack');
  if(upBack)upBack.hidden=page!=='upload';
  const appShell=$('.app-shell');
  if(appShell)appShell.classList.toggle('upload-mode',page==='upload');
  if(page==='view')render();
  if(page==='upload')initPoForm();
  window.scrollTo(0,0);
}

async function loadMasters(){
  try{
    const data=await api('/api/masters');
    state.masters=data.masters||{};
    for(const field of Object.keys(CLASS_FIELDS))state.masters[field]??=[];
    render();
    renderMasterEditors();
    initPoDropdowns();
  }catch(e){
    const ms=$('#masterStatus');
    if(ms)ms.textContent=friendlyError(e);
  }
}

function renderMasterEditors(){
  const root=$('#masterEditors');
  if(!root)return;
  root.innerHTML=Object.entries(MANAGED_FIELDS).map(([field,[label]])=>`
    <div class="master-editor">
      <div class="master-editor-title">${esc(label)}</div>
      <div class="master-entry">
        <input id="master-input-${field}" maxlength="80" placeholder="Add ${label.toLowerCase()}">
        <button type="button" class="secondary" data-master-add="${field}">Add</button>
      </div>
      <div class="master-values">
        ${(state.masters[field]||[]).map(value=>`<span class="master-chip">${esc(value)}<button type="button" data-master-remove="${field}" data-master-value="${escapeAttr(value)}" aria-label="Remove ${escapeAttr(value)}">×</button></span>`).join('')||'<small>No values added</small>'}
      </div>
    </div>
  `).join('');
}

document.querySelectorAll('[data-page-link]').forEach(b=>b.addEventListener('click',e=>{
  e.preventDefault();
  showPage(b.dataset.pageLink);
}));

const uploadBackBtn=$('#uploadBack');
if(uploadBackBtn)uploadBackBtn.addEventListener('click',()=>showPage(previousPage));

document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>b.closest('dialog')?.close()));

/* ========================================================
   PO-Based Item-Wise Document Upload Frontend
   ======================================================== */
let poItemCounter=0;
let poDocCounter=0;

const poState={
  poId:'',
  financialYear:'',
  month:'',
  remarks:'',
  isExisting:false,
  existingPoData:null,
  activeItemId:null,
  items:[]
};

function syncCurrentItemInputs(){
  document.querySelectorAll('#poItemsList input[data-item-field]').forEach(input=>{
    const field=input.dataset.itemField;
    const uid=input.dataset.itemId;
    if(field&&uid){
      const item=poState.items.find(x=>x.uid===uid);
      if(item)item[field]=input.value;
    }
  });
}

function createNewItem(){
  poItemCounter++;
  return {
    uid:'item_'+poItemCounter,
    item_code:'',
    item_name:'',
    vendor_code:'',
    vendor_name:'',
    remarks:'',
    documents:[]
  };
}

function initPoDropdowns(){
  const fySelect=$('#poFinancialYear');
  if(fySelect){
    const currentVal=fySelect.value;
    const fyValues=(state.masters.financialYear&&state.masters.financialYear.length)?state.masters.financialYear:[
      '2024-25','2025-26','2026-27','2027-28','2028-29','2029-30'
    ];
    fySelect.innerHTML='<option value="">Select Financial Year</option>'+
      fyValues.map(v=>`<option value="${escapeAttr(v)}" ${v===currentVal?'selected':''}>${esc(v)}</option>`).join('');
  }
  const monthSelect=$('#poMonth');
  if(monthSelect){
    const currentVal=monthSelect.value;
    monthSelect.innerHTML='<option value="">Select Month</option>'+
      MONTHS.map(m=>`<option value="${escapeAttr(m)}" ${m===currentVal?'selected':''}>${esc(m)}</option>`).join('');
  }
}

function initPoForm(){
  poState.poId='';
  poState.financialYear='';
  poState.month='';
  poState.remarks='';
  poState.isExisting=false;
  poState.existingPoData=null;
  poItemCounter=0;
  poDocCounter=0;
  const firstItem=createNewItem();
  poState.items=[firstItem];
  poState.activeItemId=firstItem.uid;

  const poInput=$('#poId');
  if(poInput)poInput.value='';
  const poRem=$('#poRemarks');
  if(poRem)poRem.value='';

  const badge=$('#poStatusBadge');
  if(badge){badge.hidden=true;badge.textContent='';}
  const err=$('#poUploadError');
  if(err){err.hidden=true;err.textContent='';}
  const succ=$('#poUploadSuccess');
  if(succ){succ.hidden=true;succ.textContent='';}

  initPoDropdowns();
  const fySelect=$('#poFinancialYear');
  if(fySelect){fySelect.disabled=false;fySelect.value='';}
  const mSelect=$('#poMonth');
  if(mSelect){mSelect.disabled=false;mSelect.value='';}

  renderPoItems();
}

function renderPoItems(){
  const container=$('#poItemsList');
  if(!container)return;
  const docTypes=(state.masters.documentType&&state.masters.documentType.length)?state.masters.documentType:PO_DOCUMENT_TYPES;

  let totalDocs=0;
  container.innerHTML=poState.items.map((it,idx)=>{
    totalDocs+=it.documents.length;
    const isActive=(it.uid===poState.activeItemId);
    const canRemove=poState.items.length>1;

    const itemCodeDisplay=it.item_code.trim();
    const itemNameDisplay=it.item_name.trim();
    let titleHtml='';
    if(itemCodeDisplay||itemNameDisplay){
      titleHtml=`${itemCodeDisplay?`<b>${esc(itemCodeDisplay)}</b>`:''}${itemCodeDisplay&&itemNameDisplay?' · ':''}${esc(itemNameDisplay)}`;
    }else{
      titleHtml=`<span style="color:#94a3b8;font-style:italic;">(Item details pending…)</span>`;
    }

    const vendorDisplay=(it.vendor_code||it.vendor_name)?`${esc(it.vendor_code?it.vendor_code+' · ': '')}${esc(it.vendor_name)}`:'';

    const docsHtml=it.documents.length===0
      ?`<div style="text-align:center;padding:14px 8px;font-size:13px;color:#94a3b8;background:#f8fafc;border-radius:8px;">No documents attached yet. Click 'Choose Document(s)' to attach files.</div>`
      :`<div class="po-docs-table">${it.documents.map((doc,dIdx)=>`
          <div class="po-doc-row">
            <div class="po-doc-meta">
              <span class="po-doc-name" title="${escapeAttr(doc.file.name)}">📄 ${esc(doc.file.name)}</span>
              <span class="po-doc-size">${sizeText(doc.file.size)}</span>
            </div>
            <div>
              <select class="po-doc-type-select" data-doc-type-item="${it.uid}" data-doc-type-idx="${dIdx}">
                ${docTypes.map(dt=>`<option value="${escapeAttr(dt)}" ${doc.document_type===dt?'selected':''}>${esc(dt)}</option>`).join('')}
              </select>
            </div>
            <div>
              <button type="button" class="po-doc-remove-btn" data-doc-remove-item="${it.uid}" data-doc-remove-idx="${dIdx}" title="Remove file">×</button>
            </div>
          </div>
        `).join('')}</div>`;

    return `
      <div class="po-item-card ${isActive?'is-active':'is-collapsed'}" data-item-id="${it.uid}">
        <div class="po-item-header">
          <div class="po-item-header-clickable" data-item-toggle="${it.uid}">
            <span class="po-item-num-badge">Item #${idx+1}</span>
            <div class="po-item-summary-info">
              <span class="po-item-summary-title">${titleHtml}</span>
              ${vendorDisplay?`<span class="po-item-badge-vendor">🏢 ${vendorDisplay}</span>`:''}
              <span class="po-item-badge-docs">📎 ${it.documents.length} doc${it.documents.length===1?'':'s'}</span>
            </div>
          </div>
          <div class="po-item-header-actions">
            ${!isActive
              ?`<button type="button" class="secondary po-btn-mini" data-item-expand="${it.uid}" title="Edit this item">Edit ✎</button>`
              :`<button type="button" class="secondary po-btn-mini" data-item-collapse="${it.uid}" title="Collapse this item">Collapse ▲</button>`
            }
            ${canRemove?`<button type="button" class="action danger-action po-btn-mini" data-item-remove="${it.uid}" title="Remove this item">Remove</button>`:''}
            <span class="po-item-chevron" data-item-toggle="${it.uid}">${isActive?'▲':'▼'}</span>
          </div>
        </div>

        ${isActive?`
        <div class="po-item-body">
          <div class="po-item-grid">
            <div class="po-field">
              <label>Item Code <span class="req">*</span></label>
              <input type="text" data-item-field="item_code" data-item-id="${it.uid}" value="${escapeAttr(it.item_code)}" placeholder="e.g. ITM-001" required maxlength="80">
            </div>
            <div class="po-field">
              <label>Item Name <span class="req">*</span></label>
              <input type="text" data-item-field="item_name" data-item-id="${it.uid}" value="${escapeAttr(it.item_name)}" placeholder="e.g. Surgical Gloves 7.5" required maxlength="250">
            </div>
            <div class="po-field">
              <label>Vendor Code <span class="req">*</span></label>
              <input type="text" data-item-field="vendor_code" data-item-id="${it.uid}" value="${escapeAttr(it.vendor_code)}" placeholder="e.g. VND-402" required maxlength="80">
            </div>
            <div class="po-field">
              <label>Vendor Name <span class="req">*</span></label>
              <input type="text" data-item-field="vendor_name" data-item-id="${it.uid}" value="${escapeAttr(it.vendor_name)}" placeholder="e.g. Apex Meditech Ltd" required maxlength="250">
            </div>
          </div>

          <div class="po-field">
            <label>Remarks <span class="opt">Optional</span></label>
            <input type="text" data-item-field="remarks" data-item-id="${it.uid}" value="${escapeAttr(it.remarks)}" placeholder="Optional item remarks or batch notes..." maxlength="250">
          </div>

          <div class="po-docs-box">
            <div class="po-docs-box-head">
              <b>Associated Documents (${it.documents.length})</b>
              <label class="po-file-input-label">
                <span>↑</span> Choose Document(s)
                <input type="file" multiple data-item-file-input="${it.uid}" accept=".pdf,.doc,.docx,.png,.jpg,.jpeg,.gif,.webp,.bmp,.tif,.tiff,application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document,image/*">
              </label>
            </div>
            ${docsHtml}
          </div>

          <div class="po-item-done-row">
            <button type="button" class="secondary" data-item-collapse="${it.uid}" style="font-weight:600;min-height:36px;font-size:13px;">
              ✓ Done with Item #${idx+1}
            </button>
          </div>
        </div>
        `:''}
      </div>
    `;
  }).join('');

  const status=$('#poSummaryStatus');
  if(status){
    status.innerHTML=`Ready to upload <b>${poState.items.length}</b> item${poState.items.length===1?'':'s'} with <b>${totalDocs}</b> document${totalDocs===1?'':'s'} attached.`;
  }
}

let poCheckTimer=null;
async function checkPoIdAvailability(poId){
  const badge=$('#poStatusBadge');
  const spinner=$('#poIdSpinner');
  if(!badge)return;
  const val=(poId||'').trim();
  if(!val){
    badge.hidden=true;
    poState.isExisting=false;
    poState.existingPoData=null;
    const fy=$('#poFinancialYear'), m=$('#poMonth');
    if(fy)fy.disabled=false;
    if(m)m.disabled=false;
    return;
  }
  if(spinner)spinner.hidden=false;
  try{
    const res=await api(`/api/purchase-orders/check?po_id=${encodeURIComponent(val)}`);
    if(res.exists){
      poState.isExisting=true;
      poState.existingPoData=res.po;
      badge.className='po-status-callout existing';
      badge.innerHTML=`✓ <b>Existing PO Detected:</b> '${esc(res.po.po_id)}' (Financial Year: <b>${esc(res.po.financial_year)}</b>, Month: <b>${esc(res.po.month)}</b>, ${res.po.items_count} item(s) on file). New items will be attached to this existing PO.`;
      badge.hidden=false;
      const fy=$('#poFinancialYear'), m=$('#poMonth');
      if(fy){fy.value=res.po.financial_year;fy.disabled=true;}
      if(m){m.value=res.po.month;m.disabled=true;}
    }else{
      poState.isExisting=false;
      poState.existingPoData=null;
      badge.className='po-status-callout new';
      badge.innerHTML=`★ <b>New Purchase Order:</b> Please select Financial Year and Month below.`;
      badge.hidden=false;
      const fy=$('#poFinancialYear'), m=$('#poMonth');
      if(fy)fy.disabled=false;
      if(m)m.disabled=false;
    }
  }catch(err){
    badge.className='po-status-callout';
    badge.textContent=err.message;
    badge.hidden=false;
  }finally{
    if(spinner)spinner.hidden=true;
  }
}

const poIdInput=$('#poId');
if(poIdInput){
  poIdInput.addEventListener('input',()=>{
    clearTimeout(poCheckTimer);
    poCheckTimer=setTimeout(()=>checkPoIdAvailability(poIdInput.value),350);
  });
  poIdInput.addEventListener('blur',()=>{
    clearTimeout(poCheckTimer);
    checkPoIdAvailability(poIdInput.value);
  });
}

const poItemsList=$('#poItemsList');
if(poItemsList){
  poItemsList.addEventListener('input',e=>{
    const field=e.target.dataset.itemField;
    const uid=e.target.dataset.itemId;
    if(!field||!uid)return;
    const item=poState.items.find(x=>x.uid===uid);
    if(item)item[field]=e.target.value;
  });

  poItemsList.addEventListener('change',e=>{
    const fileInputUid=e.target.dataset.itemFileInput;
    if(fileInputUid){
      syncCurrentItemInputs();
      const files=Array.from(e.target.files||[]);
      if(!files.length)return;
      const item=poState.items.find(x=>x.uid===fileInputUid);
      if(!item)return;
      const allowed=['pdf','doc','docx','png','jpg','jpeg','gif','webp','bmp','tif','tiff'];
      for(const f of files){
        if(f.size>MAX_SIZE){
          toast(`File '${f.name}' exceeds the 250 MB limit.`);
          continue;
        }
        if(!allowed.includes(extOf(f.name))){
          toast(`File '${f.name}' has an unsupported file type.`);
          continue;
        }
        poDocCounter++;
        item.documents.push({
          id:'doc_'+poDocCounter,
          file:f,
          document_type:(state.masters.documentType&&state.masters.documentType[0])||'Purchase Order'
        });
      }
      renderPoItems();
      return;
    }

    const typeItemUid=e.target.dataset.docTypeItem;
    const typeIdx=e.target.dataset.docTypeIdx;
    if(typeItemUid&&typeIdx!==undefined){
      const item=poState.items.find(x=>x.uid===typeItemUid);
      if(item&&item.documents[+typeIdx]){
        item.documents[+typeIdx].document_type=e.target.value;
      }
    }
  });

  poItemsList.addEventListener('click',e=>{
    // 1. Remove Item
    const removeBtn=e.target.closest('[data-item-remove]');
    if(removeBtn){
      syncCurrentItemInputs();
      const uid=removeBtn.dataset.itemRemove;
      poState.items=poState.items.filter(x=>x.uid!==uid);
      if(poState.items.length===0){
        const newItem=createNewItem();
        poState.items.push(newItem);
        poState.activeItemId=newItem.uid;
      }else if(poState.activeItemId===uid){
        poState.activeItemId=poState.items[poState.items.length-1].uid;
      }
      renderPoItems();
      return;
    }

    // 2. Remove document
    const removeDocBtn=e.target.closest('[data-doc-remove-item]');
    if(removeDocBtn){
      syncCurrentItemInputs();
      const itemUid=removeDocBtn.dataset.docRemoveItem;
      const docIdx=+removeDocBtn.dataset.docRemoveIdx;
      const item=poState.items.find(x=>x.uid===itemUid);
      if(item){
        item.documents.splice(docIdx,1);
        renderPoItems();
      }
      return;
    }

    // 3. Edit / Expand item
    const expandBtn=e.target.closest('[data-item-expand]');
    if(expandBtn){
      syncCurrentItemInputs();
      poState.activeItemId=expandBtn.dataset.itemExpand;
      renderPoItems();
      const firstInput=$(`#poItemsList [data-item-id="${poState.activeItemId}"][data-item-field="item_code"]`);
      if(firstInput)firstInput.focus();
      return;
    }

    // 4. Collapse item
    const collapseBtn=e.target.closest('[data-item-collapse]');
    if(collapseBtn){
      syncCurrentItemInputs();
      poState.activeItemId=null;
      renderPoItems();
      return;
    }

    // 5. Toggle item on header click
    const toggleTarget=e.target.closest('[data-item-toggle]');
    if(toggleTarget){
      syncCurrentItemInputs();
      const uid=toggleTarget.dataset.itemToggle;
      if(poState.activeItemId===uid){
        poState.activeItemId=null;
      }else{
        poState.activeItemId=uid;
      }
      renderPoItems();
    }
  });
}

const btnAddItem=$('#btnAddItem');
if(btnAddItem){
  btnAddItem.addEventListener('click',()=>{
    syncCurrentItemInputs();
    const newItem=createNewItem();
    poState.items.push(newItem);
    poState.activeItemId=newItem.uid;
    renderPoItems();
    setTimeout(()=>{
      const card=$(`#poItemsList [data-item-id="${newItem.uid}"]`);
      if(card){
        card.scrollIntoView({behavior:'smooth',block:'nearest'});
        const codeInput=card.querySelector('[data-item-field="item_code"]');
        if(codeInput)codeInput.focus();
      }
    },50);
  });
}

const poUploadForm=$('#poUploadForm');
if(poUploadForm){
  poUploadForm.addEventListener('submit',async e=>{
    e.preventDefault();
    const errBox=$('#poUploadError');
    const succBox=$('#poUploadSuccess');
    if(errBox)errBox.hidden=true;
    if(succBox)succBox.hidden=true;

    const poId=($('#poId')?.value||'').trim();
    if(!poId){
      if(errBox){errBox.textContent='Please enter a Purchase Order (PO) ID.';errBox.hidden=false;}
      $('#poId')?.focus();
      return;
    }

    const finYear=$('#poFinancialYear')?.value;
    const month=$('#poMonth')?.value;
    if(!poState.isExisting&&(!finYear||!month)){
      if(errBox){errBox.textContent='Please select both Financial Year and Month for this Purchase Order.';errBox.hidden=false;}
      return;
    }

    if(!poState.items.length){
      if(errBox){errBox.textContent='At least one item is required under the Purchase Order.';errBox.hidden=false;}
      return;
    }

    syncCurrentItemInputs();

    for(let i=0;i<poState.items.length;i++){
      const it=poState.items[i];
      const num=i+1;
      if(!it.item_code.trim()){
        poState.activeItemId=it.uid;
        renderPoItems();
        if(errBox){errBox.textContent=`Please enter Item Code for Item #${num}.`;errBox.hidden=false;}
        const el=$(`#poItemsList [data-item-id="${it.uid}"][data-item-field="item_code"]`);
        if(el)el.focus();
        return;
      }
      if(!it.item_name.trim()){
        poState.activeItemId=it.uid;
        renderPoItems();
        if(errBox){errBox.textContent=`Please enter Item Name for Item #${num}.`;errBox.hidden=false;}
        const el=$(`#poItemsList [data-item-id="${it.uid}"][data-item-field="item_name"]`);
        if(el)el.focus();
        return;
      }
      if(!it.vendor_code.trim()){
        poState.activeItemId=it.uid;
        renderPoItems();
        if(errBox){errBox.textContent=`Please enter Vendor Code for Item #${num}.`;errBox.hidden=false;}
        const el=$(`#poItemsList [data-item-id="${it.uid}"][data-item-field="vendor_code"]`);
        if(el)el.focus();
        return;
      }
      if(!it.vendor_name.trim()){
        poState.activeItemId=it.uid;
        renderPoItems();
        if(errBox){errBox.textContent=`Please enter Vendor Name for Item #${num}.`;errBox.hidden=false;}
        const el=$(`#poItemsList [data-item-id="${it.uid}"][data-item-field="vendor_name"]`);
        if(el)el.focus();
        return;
      }
      if(!it.documents.length){
        poState.activeItemId=it.uid;
        renderPoItems();
        if(errBox){errBox.textContent=`Please attach at least one document for Item #${num} ('${it.item_name||it.item_code}').`;errBox.hidden=false;}
        return;
      }
      for(const d of it.documents){
        if(!d.document_type){
          poState.activeItemId=it.uid;
          renderPoItems();
          if(errBox){errBox.textContent=`Please select Document Type for all files under Item #${num}.`;errBox.hidden=false;}
          return;
        }
      }
    }

    const submitBtn=$('#poSubmitBtn');
    const origText=submitBtn?submitBtn.textContent:'Submit Upload';
    if(submitBtn){submitBtn.disabled=true;submitBtn.textContent='Uploading…';}

    const formData=new FormData();
    const poPayload={
      po_id:poId,
      financial_year:finYear,
      month:month,
      remarks:($('#poRemarks')?.value||'').trim(),
      items:[]
    };

    poState.items.forEach((it,iIdx)=>{
      const itemData={
        item_code:it.item_code.trim(),
        item_name:it.item_name.trim(),
        vendor_code:it.vendor_code.trim(),
        vendor_name:it.vendor_name.trim(),
        remarks:it.remarks.trim(),
        documents:[]
      };
      it.documents.forEach((d,dIdx)=>{
        const fileKey=`file_${iIdx}_${dIdx}`;
        formData.append(fileKey,d.file);
        itemData.documents.push({
          file_key:fileKey,
          document_type:d.document_type
        });
      });
      poPayload.items.push(itemData);
    });

    formData.append('po_data',JSON.stringify(poPayload));

    try{
      const result=await api('/api/purchase-orders/upload',{
        method:'POST',
        body:formData
      });
      if(succBox){
        succBox.innerHTML=`✓ <b>Success:</b> ${esc(result.message||'Upload completed successfully.')}`;
        succBox.hidden=false;
      }
      toast('PO upload completed successfully.');
      initPoForm();
      await refresh();
    }catch(err){
      if(errBox){
        errBox.textContent=friendlyError(err)||err.message;
        errBox.hidden=false;
      }
    }finally{
      if(submitBtn){submitBtn.disabled=false;submitBtn.textContent=origText;}
    }
  });
}

/* ========================================================
   Search & Filter Listeners (PO ID & Financial Year ONLY)
   ======================================================== */
const fieldFiltersBox=$('#fieldFilters');
if(fieldFiltersBox){
  fieldFiltersBox.addEventListener('change',e=>{
    const field=e.target.dataset.metaFilter;
    if(field){
      state.metaFilter[field]=e.target.value.trim();
      renderDocumentList();
    }
  });

  fieldFiltersBox.addEventListener('input',e=>{
    const field=e.target.dataset.metaFilter;
    if(field&&(e.target.tagName==='INPUT'||e.target.tagName==='SELECT')){
      state.metaFilter[field]=e.target.value.trim();
      renderDocumentList();
    }
  });
}

const clearFiltersBtn=$('#clearFilters');
if(clearFiltersBtn)clearFiltersBtn.addEventListener('click',()=>{
  state.filter=[];
  state.metaFilter={};
  const poInput=$('#searchPoId');
  if(poInput){poInput.value='';}
  const fySelect=$('#searchFinYear');
  if(fySelect){fySelect.value='';}
  renderDocumentList();
});

const docListEl=$('#documentList');
if(docListEl){
  docListEl.addEventListener('click',e=>{
    const b=e.target.closest('[data-view]');
    if(b){
      const d=state.docs.find(x=>x.id===b.dataset.view);
      if(d)openView(d);
    }
    const edit=e.target.closest('[data-edit]'), del=e.target.closest('[data-delete]');
    if(edit){
      const d=state.docs.find(x=>x.id===edit.dataset.edit);
      if(!d)return;
      $('#editDocumentId').value=d.id;
      $('#editFields').innerHTML=Object.entries(CLASS_FIELDS).map(([field,[label]])=>{
        const val=d[field]||'';
        if(field==='documentType'||field==='financialYear'||field==='month'){
          const values=[...new Set([...(field==='month'?MONTHS:(state.masters[field]||[])),val].filter(Boolean))];
          return `<label>${esc(label)}<select name="${field}"><option value="">—</option>${values.map(v=>`<option ${v===val?'selected':''} value="${escapeAttr(v)}">${esc(v)}</option>`).join('')}</select></label>`;
        }
        return `<label>${esc(label)}<input name="${field}" maxlength="250" value="${escapeAttr(val)}"></label>`;
      }).join('');
      $('#editDialog').showModal();
    }
    if(del){
      if(!confirm('Delete this document and its stored attachment?'))return;
      (async()=>{
        try{
          await api(`/api/documents/${encodeURIComponent(del.dataset.delete)}/delete`,{method:'POST'});
          await refresh();
          toast('Document deleted.');
        }catch(err){toast(err.message);}
      })();
    }
  });
}

const editForm=$('#editForm');
if(editForm){
  editForm.addEventListener('submit',async e=>{
    e.preventDefault();
    const id=$('#editDocumentId').value,data=Object.fromEntries(new FormData(e.target).entries());
    try{
      await api(`/api/documents/${encodeURIComponent(id)}/edit`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
      $('#editDialog').close();
      await refresh();
      toast('Document details updated.');
    }catch(err){
      $('#editError').textContent=err.message;
      $('#editError').hidden=false;
    }
  });
}

/* ========================================================
   Settings & Masters
   ======================================================== */
window.addEventListener('resize',updateNetworkStatus);
const logsOpenBtn=$('#logsOpen');
if(logsOpenBtn)logsOpenBtn.addEventListener('click',()=>{
  $('#logsDialog')?.showModal();
  loadLogs();
});
const refreshLogsBtn=$('#refreshLogs');
if(refreshLogsBtn)refreshLogsBtn.addEventListener('click',loadLogs);
const retryConnBtn=$('#connectionRetry');
if(retryConnBtn)retryConnBtn.addEventListener('click',refresh);

const copyLogsBtn=$('#copyLogs');
if(copyLogsBtn)copyLogsBtn.addEventListener('click',async()=>{
  const logs=await getAllLogs();
  const text=logs.length?logs.map(x=>`${localTime(x.timestamp)} | ${x.level} | ${x.action} | ${x.message}`).join('\n'):'No failure logs recorded.';
  try{await navigator.clipboard.writeText(text);toast('Logs copied to clipboard.');}catch{
    const area=document.createElement('textarea');area.value=text;document.body.append(area);area.select();
    document.execCommand('copy');area.remove();toast('Logs copied.');
  }
});

function syncSettingsFields(){
  const dbVal=$('#dbBackend')?.value;
  if($('#postgresFields'))$('#postgresFields').hidden=dbVal!=='postgres';
  const fsVal=$('#fileBackend')?.value;
  if($('#smbFields'))$('#smbFields').hidden=fsVal!=='smb';
}

async function loadStorageSettings(){
  try{
    const cfg=await api('/api/storage/settings');
    state.databaseBackend=cfg.database.backend;
    state.fileBackend=cfg.files.backend;
    state.smbMounted=!!cfg.smbMounted;
    updateNetworkStatus();
    if($('#dbBackend'))$('#dbBackend').value=cfg.database.backend;
    if($('#pgHost'))$('#pgHost').value=cfg.database.host||'';
    if($('#pgPort'))$('#pgPort').value=cfg.database.port||5432;
    if($('#pgName'))$('#pgName').value=cfg.database.name||'';
    if($('#pgUser'))$('#pgUser').value=cfg.database.user||'';
    if($('#pgSsl'))$('#pgSsl').value=cfg.database.sslmode||'prefer';
    if($('#fileBackend'))$('#fileBackend').value=cfg.files.backend;
    if($('#smbHost'))$('#smbHost').value=cfg.files.host||'';
    if($('#smbShare'))$('#smbShare').value=cfg.files.share||'';
    syncSettingsFields();
  }catch(e){
    if($('#settingsError')){
      $('#settingsError').textContent=friendlyError(e);
      $('#settingsError').hidden=false;
    }
  }
}

const storageSettingsOpenBtn=$('#storageSettingsOpen');
if(storageSettingsOpenBtn){
  storageSettingsOpenBtn.addEventListener('click',()=>{
    $('#settingsError').hidden=true;
    $('#storageSettingsDialog')?.showModal();
    loadStorageSettings();
    loadMasters();
  });
}
$('#dbBackend')?.addEventListener('change',syncSettingsFields);
$('#fileBackend')?.addEventListener('change',syncSettingsFields);

const masterEditorsBox=$('#masterEditors');
if(masterEditorsBox){
  masterEditorsBox.addEventListener('click',e=>{
    const add=e.target.closest('[data-master-add]'),remove=e.target.closest('[data-master-remove]');
    if(add){
      const field=add.dataset.masterAdd,input=$(`#master-input-${field}`),value=input.value.trim().replace(/\s+/g,' ');
      if(!value)return;
      if(value.length>80){$('#masterStatus').textContent='Values can contain up to 80 characters.';return;}
      const values=state.masters[field]||(state.masters[field]=[]);
      if(!values.some(x=>x.toLowerCase()===value.toLowerCase()))values.push(value);
      input.value='';
      $('#masterStatus').textContent='Unsaved changes';
      renderMasterEditors();
      render();
    }else if(remove){
      const field=remove.dataset.masterRemove,value=remove.dataset.masterValue;
      state.masters[field]=(state.masters[field]||[]).filter(x=>x!==value);
      $('#masterStatus').textContent='Unsaved changes';
      renderMasterEditors();
      render();
    }
  });

  masterEditorsBox.addEventListener('keydown',e=>{
    if(e.key==='Enter'&&e.target.id.startsWith('master-input-')){
      e.preventDefault();
      e.target.closest('.master-editor')?.querySelector('[data-master-add]')?.click();
    }
  });
}

const saveMastersBtn=$('#saveMasters');
if(saveMastersBtn){
  saveMastersBtn.addEventListener('click',async()=>{
    saveMastersBtn.disabled=true;
    $('#masterStatus').textContent='Saving…';
    try{
      const result=await api('/api/masters',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({masters:state.masters})});
      state.masters=result.masters;
      renderMasterEditors();
      render();
      initPoDropdowns();
      renderPoItems();
      $('#masterStatus').textContent='Masters saved.';
      toast('Classification masters saved.');
    }catch(e){
      $('#masterStatus').textContent=e.message;
    }finally{
      saveMastersBtn.disabled=false;
    }
  });
}

const connectSmbBtn=$('#connectSmb');
if(connectSmbBtn){
  connectSmbBtn.addEventListener('click',async()=>{
    connectSmbBtn.disabled=true;
    $('#smbStatus').textContent='Opening the shared folder…';
    try{
      await api('/api/storage/smb/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({host:$('#smbHost').value,share:$('#smbShare').value})});
      $('#smbStatus').textContent='Sign in to the share in File Explorer or Finder. After it opens, save these settings.';
    }catch(e){
      $('#smbStatus').textContent=friendlyError(e);
      $('#settingsError').textContent=e.message;
      $('#settingsError').hidden=false;
    }finally{
      connectSmbBtn.disabled=false;
    }
  });
}

const settingsForm=$('#settingsForm');
if(settingsForm){
  settingsForm.addEventListener('submit',async e=>{
    e.preventDefault();
    $('#settingsError').hidden=true;
    const payload={database:{backend:$('#dbBackend').value},files:{backend:$('#fileBackend').value}};
    if(payload.database.backend==='postgres'){
      Object.assign(payload.database,{host:$('#pgHost').value,port:Number($('#pgPort').value),name:$('#pgName').value,user:$('#pgUser').value,password:$('#pgPassword').value,sslmode:$('#pgSsl').value});
    }
    if(payload.files.backend==='smb'){
      Object.assign(payload.files,{host:$('#smbHost').value,share:$('#smbShare').value});
    }
    const b=$('#settingsSave');
    b.disabled=true;
    b.textContent='Checking…';
    try{
      await api('/api/storage/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      $('#storageSettingsDialog').close();
      $('#pgPassword').value='';
      toast('Storage settings saved.');
      await refresh();
    }catch(err){
      $('#settingsError').textContent=err.message;
      $('#settingsError').hidden=false;
    }finally{
      b.disabled=false;
      b.textContent='Save settings';
    }
  });
}

async function loadUsers(){
  try{
    const data=await api('/api/users');
    const userList=$('#userList');
    if(userList){
      userList.innerHTML=data.users.map(u=>`<div class="user-row"><b>${esc(u.username)}</b><span>${u.role==='admin'?'Admin':'Upload user'}</span><button class="action danger-action" data-user-delete="${escapeAttr(u.id)}">Remove</button></div>`).join('');
    }
  }catch(e){
    const us=$('#userStatus');
    if(us)us.textContent=e.message;
  }
}

const createUserBtn=$('#createUser');
if(createUserBtn){
  createUserBtn.addEventListener('click',async()=>{
    const username=$('#newUsername').value,password=$('#newUserPassword').value,role=$('#newUserRole').value;
    try{
      await api('/api/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password,role})});
      $('#newUsername').value='';
      $('#newUserPassword').value='';
      $('#userStatus').textContent='User added.';
      loadUsers();
    }catch(e){
      $('#userStatus').textContent=e.message;
    }
  });
}

const userListEl=$('#userList');
if(userListEl){
  userListEl.addEventListener('click',async e=>{
    const b=e.target.closest('[data-user-delete]');
    if(!b)return;
    if(!confirm('Remove this user account?'))return;
    try{
      await api(`/api/users/${encodeURIComponent(b.dataset.userDelete)}/delete`,{method:'POST'});
      loadUsers();
    }catch(err){
      $('#userStatus').textContent=err.message;
    }
  });
}

/* ========================================================
   Authentication Flow
   ======================================================== */
function enterApp(user){
  state.currentUser=user;
  const gate=$('#authGate');
  if(gate){gate.hidden=true;gate.style.display='none';}
  const header=$('#mainHeader');
  if(header){header.hidden=false;header.style.display='flex';}
  const app=$('#mainApp');
  if(app){app.hidden=false;app.style.display='block';}
  const badge=$('#userBadge');
  if(badge)badge.textContent=`${user.username} · ${user.role==='admin'?'Admin':'Upload user'}`;
  document.querySelectorAll('.admin-only').forEach(el=>el.hidden=(user.role!=='admin'));
  const lo=$('#logsOpen');if(lo)lo.hidden=(user.role!=='admin');
  const ee=$('#exportExcel');if(ee)ee.hidden=(user.role!=='admin');
  showPage('home');
  loadMasters();
  refresh();
  if(user.role==='admin')loadUsers();
}

function setAuthMode(mode){
  state.authMode=mode;
  const err=$('#authError');
  if(err){err.hidden=true;err.textContent='';}
  const title=$('#authTitle'),help=$('#authHelp'),submit=$('#authSubmit'),confirmWrap=$('#authConfirmWrap'),pwd=$('#authPassword'),switchWrap=$('#authSwitchWrap'),switchText=$('#authSwitchText'),switchBtn=$('#authSwitchBtn');
  if(mode==='setup'){
    if(title)title.textContent='Create administrator';
    if(help)help.textContent='Set up the first administrator account for this document register.';
    if(submit)submit.textContent='Create administrator';
    if(confirmWrap)confirmWrap.hidden=false;
    if(pwd)pwd.autocomplete='new-password';
    if(switchWrap)switchWrap.hidden=true;
  }else if(mode==='signup'){
    if(title)title.textContent='Sign up';
    if(help)help.textContent='Create a new account to access the document register.';
    if(submit)submit.textContent='Sign up';
    if(confirmWrap)confirmWrap.hidden=false;
    if(pwd)pwd.autocomplete='new-password';
    if(switchWrap){
      switchWrap.hidden=false;
      if(switchText)switchText.textContent='Already have an account?';
      if(switchBtn)switchBtn.textContent='Sign in';
    }
  }else{
    if(title)title.textContent='Sign in';
    if(help)help.textContent='Sign in to access the document register.';
    if(submit)submit.textContent='Sign in';
    if(confirmWrap)confirmWrap.hidden=true;
    if(pwd)pwd.autocomplete='current-password';
    if(switchWrap){
      switchWrap.hidden=false;
      if(switchText)switchText.textContent="Don't have an account?";
      if(switchBtn)switchBtn.textContent='Sign up';
    }
  }
}

const authSwitchBtn=$('#authSwitchBtn');
if(authSwitchBtn){
  authSwitchBtn.addEventListener('click',()=>{
    setAuthMode(state.authMode==='signup'?'login':'signup');
  });
}

async function bootAuth(){
  try{
    const status=await api('/api/auth/status');
    if(status.user){
      enterApp(status.user);
      return;
    }
    state.setupRequired=!!status.setupRequired;
    setAuthMode(status.setupRequired?'setup':'login');
    const gate=$('#authGate');
    if(gate){gate.hidden=false;gate.style.display='grid';}
    const header=$('#mainHeader');
    if(header){header.hidden=true;header.style.display='none';}
    const app=$('#mainApp');
    if(app){app.hidden=true;app.style.display='none';}
  }catch(e){
    const err=$('#authError');
    if(err){err.textContent=friendlyError(e);err.hidden=false;}
  }
}

const authForm=$('#authForm');
if(authForm){
  authForm.addEventListener('submit',async e=>{
    e.preventDefault();
    const errBox=$('#authError');
    if(errBox)errBox.hidden=true;
    const username=($('#authUsername')?.value||'').trim();
    const password=$('#authPassword')?.value||'';
    const confirmPassword=$('#authConfirm')?.value||'';

    if(!username){
      if(errBox){errBox.textContent='Please enter a username.';errBox.hidden=false;}
      return;
    }
    if(!password){
      if(errBox){errBox.textContent='Please enter a password.';errBox.hidden=false;}
      return;
    }

    let endpoint='/api/auth/login';
    const payload={username,password};

    if(state.authMode==='setup'||state.setupRequired){
      endpoint='/api/auth/setup';
      if(!confirmPassword){
        if(errBox){errBox.textContent='Please confirm your password.';errBox.hidden=false;}
        return;
      }
      if(password!==confirmPassword){
        if(errBox){errBox.textContent='Passwords do not match.';errBox.hidden=false;}
        return;
      }
      payload.confirmPassword=confirmPassword;
    }else if(state.authMode==='signup'){
      endpoint='/api/auth/signup';
      if(!confirmPassword){
        if(errBox){errBox.textContent='Please confirm your password.';errBox.hidden=false;}
        return;
      }
      if(password!==confirmPassword){
        if(errBox){errBox.textContent='Passwords do not match.';errBox.hidden=false;}
        return;
      }
      payload.confirmPassword=confirmPassword;
    }

    const submitBtn=$('#authSubmit');
    const origText=submitBtn?submitBtn.textContent:'Sign in';
    if(submitBtn){submitBtn.disabled=true;submitBtn.textContent='Please wait…';}

    try{
      const result=await api(endpoint,{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload)
      });
      authForm.reset();
      enterApp(result.user);
    }catch(err){
      if(errBox){
        errBox.textContent=friendlyError(err)||err.message;
        errBox.hidden=false;
      }
    }finally{
      if(submitBtn){submitBtn.disabled=false;submitBtn.textContent=origText;}
    }
  });
}

const signOutBtn=$('#signOut');
if(signOutBtn){
  signOutBtn.addEventListener('click',async()=>{
    try{await api('/api/auth/logout',{method:'POST'});}catch{}
    state.currentUser=null;
    const app=$('#mainApp');
    if(app){app.hidden=true;app.style.display='none';}
    const header=$('#mainHeader');
    if(header){header.hidden=true;header.style.display='none';}
    state.setupRequired=false;
    setAuthMode('login');
    const gate=$('#authGate');
    if(gate){gate.hidden=false;gate.style.display='grid';}
  });
}

bootAuth();
