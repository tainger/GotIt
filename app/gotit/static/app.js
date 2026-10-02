const $ = s => document.querySelector(s);
let token = '', result = null, lastJobs = '', polling = false;
const labels = {queued:'等待下载',downloading:'正在下载',done:'已保存到电脑',failed:'下载失败'};
function message(text, error=false) { $('#message').textContent=text; $('#message').classList.toggle('error',error); }
async function api(path, body) {
  const response = await fetch(path, body ? {method:'POST',headers:{'Content-Type':'application/json','X-Gotit-Token':token},body:JSON.stringify(body)} : {});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail==='string' ? data.detail : '请求未完成，请检查输入后重试。');
  return data;
}
function node(tag, text, className) { const n=document.createElement(tag); if(text)n.textContent=text; if(className)n.className=className; return n; }
function mb(n) {return (n/1024/1024).toFixed(1)+' MB';}
async function refresh() {
  if(polling)return;
  polling=true;
  try {
    const jobs = await api('/api/tasks');
    $('#count').textContent = jobs.filter(j=>j.status==='done').length+' 个视频';
    const signature=JSON.stringify(jobs); if(signature===lastJobs)return; lastJobs=signature;
    if(!jobs.length)return;
    $('#jobs').replaceChildren();
    for(const job of jobs) {
      const row=node('article',null,'job '+job.status), icon=node('div',job.status==='done'?'✓':'↓','job-icon'), body=node('div',null,'job-body');
      body.append(node('h3',job.title));
      let state=labels[job.status];
      if(job.status==='downloading') state+=' · '+mb(job.downloaded)+(job.total?' / '+mb(job.total):'');
      if(job.status==='done')state+=' · '+mb(job.downloaded);
      body.append(node('p',job.error||state));
      if(job.status==='downloading') {const p=node('progress'); if(job.total){p.max=job.total;p.value=job.downloaded;} p.setAttribute('aria-label','下载进度');body.append(p);}
      row.append(icon,body);
      if(job.status==='done') {
        const actions=node('div',null,'job-actions'), reveal=node('button','打开文件位置','secondary'), preview=node('button','预览','secondary'), save=node('a','另存文件','file-link');
        reveal.onclick=async()=>{
          reveal.disabled=true;
          try {await api('/api/files/'+job.id+'/reveal',{});message('已在 Finder 中选中文件。');}
          catch(error){message(error.message,true);}
          finally{reveal.disabled=false;}
        };
        preview.onclick=()=>{$('#preview video').src='/api/files/'+job.id+'?preview=true';$('#preview').showModal();};
        save.href='/api/files/'+job.id; actions.append(reveal,preview,save); row.append(actions);
      }
      $('#jobs').append(row);
    }
  } catch(error) {message('连接已中断，请确认本地程序仍在运行。',true);}
  finally {polling=false;}
}
$('#resolve-form').addEventListener('submit',async event=>{
  event.preventDefault(); $('#resolve').disabled=true; $('#result').hidden=true; result=null;
  message('正在解析，必要时会用独立浏览器加载视频，可能需要约一分钟…');
  try {result=await api('/api/resolve',{text:$('#link').value});$('#video-title').textContent=result.title;$('#video-author').textContent=result.author?'作者 / '+result.author:'';$('#result').hidden=false;message('解析完成，可以下载。');}
  catch(error){message(error.message,true);}finally{$('#resolve').disabled=false;}
});
$('#download').onclick=async()=>{
  if(!result)return;$('#download').disabled=true;
  try {await api('/api/download',{result_id:result.id});message('下载任务已提交，进度显示在下方。');await refresh();}
  catch(error){message(error.message,true);}finally{$('#download').disabled=false;}
};
$('#close-preview').onclick=()=>$('#preview').close();
$('#preview').addEventListener('close',()=>{$('#preview video').pause();$('#preview video').removeAttribute('src');$('#preview video').load();});
async function init(){
  try {const config=await api('/api/config');token=config.token;$('#directory').textContent=config.download_dir;await refresh();setInterval(refresh,1500);}
  catch(error){message('无法连接本地服务，请重新启动程序后刷新页面。',true);}
}
init();
