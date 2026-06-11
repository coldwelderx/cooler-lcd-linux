(function(){
  var b=document.getElementById('themeBtn');
  if(b){b.addEventListener('click',function(){
    var l=document.documentElement.getAttribute('data-theme')==='light';
    if(l){document.documentElement.removeAttribute('data-theme');try{localStorage.setItem('cl-theme','dark');}catch(e){}}
    else{document.documentElement.setAttribute('data-theme','light');try{localStorage.setItem('cl-theme','light');}catch(e){}}
  });}
  var o=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting){e.target.classList.add('in');o.unobserve(e.target);}});},{threshold:.14});
  document.querySelectorAll('.rv').forEach(function(el){o.observe(el);});
  document.querySelectorAll('[data-copy]').forEach(function(btn){
    btn.addEventListener('click',function(){
      var t=document.getElementById(btn.getAttribute('data-copy')); if(!t)return;
      var txt=t.textContent.trim();
      if(navigator.clipboard){navigator.clipboard.writeText(txt).then(function(){var old=btn.textContent;btn.textContent='Copied';setTimeout(function(){btn.textContent=old;},1400);});}
    });
  });
})();
