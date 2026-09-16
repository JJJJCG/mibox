/* mibox 前端共用脚本：请求封装、会话号、轻提示、小工具
 * 主页面（index.html）与后台页（admin.html）都引入它。 */

// 每个标签页一个会话号：本机播放的队列与播放状态按会话隔离，所以一台设备上的
// 操作不会影响另一台。放在 sessionStorage 而不是 localStorage，是为了让同一台
// 设备开多个标签页时也各播各的——共用一个会话的话两个页面会同时出声。
const SID = (() => {
  let v = sessionStorage.getItem('mibox.sid');
  if(!v){
    v = Math.random().toString(36).slice(2,10) + Date.now().toString(36).slice(-4);
    sessionStorage.setItem('mibox.sid', v);
  }
  return v;
})();

async function api(url, opts){
  opts = opts || {};
  opts.headers = Object.assign({'X-Mibox-Session': SID}, opts.headers || {});
  // 后台口令存在 cookie 里，同源请求浏览器会自动带上
  const r = await fetch(url, opts);
  if(!r.ok){ throw new Error((await r.text()).slice(0,200)); }
  return r.json();
}
const post = (u,b) => api(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});

function esc(s){
  return String(s==null?'':s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
const fmt = s => `${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`;

let toastTimer = null;
function toast(msg, isErr){
  const el = document.getElementById('toast');
  if(!el) return;
  el.textContent = msg;
  el.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>{ el.className = 'toast'; }, 2400);
}
