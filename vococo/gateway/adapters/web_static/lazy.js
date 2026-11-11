// 普通同源脚本保留全局声明、版本哈希和 SW 缓存策略,不使用 eval/blob。
const vococoLazy = (()=>{
  const assets=JSON.parse(document.getElementById("lazyAssets").textContent);
  const labels={settings:"设置", workbench:"工作台", library:"作品库"};
  const loads=new Map(), completed=new Set();
  let intent=0;

  function load(name){
    if(loads.has(name)) return loads.get(name);
    if(!assets[name]) return Promise.reject(new Error("未知模块:"+name));
    const script=document.createElement("script");
    const promise=new Promise((resolve,reject)=>{
      script.src=assets[name];
      script.onload=()=>{
        if(completed.has(name)){ resolve(); return; }
        // 执行失败可能已声明全局变量,不能盲目再次执行、重复绑定监听器。
        reject(new Error("脚本初始化失败,请刷新页面后重试"));
      };
      script.onerror=()=>{
        loads.delete(name); script.remove();
        reject(new Error("资源下载失败,请检查网络后再次点击"));
      };
    });
    loads.set(name,promise);
    document.head.append(script);
    return promise;
  }

  async function run(name, action){
    const current=++intent;
    try{
      await load(name);
      if(current===intent) await action();
    }catch(error){
      if(current===intent) alert(labels[name]+"加载失败:"+error.message);
    }
  }

  return {load, run, ready:name=>completed.add(name), cancel:()=>{ intent++; }};
})();
