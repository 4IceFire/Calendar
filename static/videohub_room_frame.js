/* Full-image canvas: percentages and drag coordinates share the displayed image bounds. */
(function(){
  'use strict';
  var sizes = Object.create(null);
  function fit(stage){
    var url=stage.dataset.fullRoomImage, size=sizes[url]; if(!size || !stage.isConnected) return;
    var parent=stage.parentElement, style=getComputedStyle(parent);
    var frame=getComputedStyle(stage), borderX=(parseFloat(frame.borderLeftWidth)||0)+(parseFloat(frame.borderRightWidth)||0), borderY=(parseFloat(frame.borderTopWidth)||0)+(parseFloat(frame.borderBottomWidth)||0);
    var available=Math.max(1,parent.clientWidth-(parseFloat(style.paddingLeft)||0)-(parseFloat(style.paddingRight)||0)-borderX);
    var cap=innerWidth>=992?parseFloat(getComputedStyle(stage).getPropertyValue('--vh-stage-height')):0;
    if(!cap) cap=Math.max(240,Math.min(700,innerHeight*(stage.classList.contains('vhm-stage') ? 0.42 : 0.62)));
    var width=Math.min(available,cap*size[0]/size[1]);
    stage.style.setProperty('width',(width+borderX)+'px');
    stage.style.setProperty('height',(width*size[1]/size[0]+borderY)+'px','important');
    stage.style.setProperty('min-height','0','important');stage.style.setProperty('flex','none','important');
    stage.style.marginLeft='auto';stage.style.marginRight='auto';stage.style.maxWidth='100%';
    stage.style.backgroundSize='100% 100%';stage.style.backgroundRepeat='no-repeat';stage.dataset.imageReady='true';
  }
  window.fitVideohubRoomImage=function(stage,url){
    stage.style.backgroundImage=url?'url('+JSON.stringify(url)+')':'none';
    if(!url){
      delete stage.dataset.fullRoomImage;delete stage.dataset.imageReady;
      ['width','height','min-height','flex','margin-left','margin-right','max-width','background-size','background-repeat'].forEach(function(key){stage.style.removeProperty(key);});return;
    }
    stage.dataset.fullRoomImage=url;stage.dataset.imageReady='false';
    if(sizes[url]){fit(stage);return;}
    var image=new Image();image.onload=function(){if(image.naturalWidth&&image.naturalHeight){sizes[url]=[image.naturalWidth,image.naturalHeight];if(stage.dataset.fullRoomImage===url)fit(stage);}};
    image.onerror=function(){if(stage.dataset.fullRoomImage===url)stage.dataset.imageReady='false';};image.src=url;
  };
  window.addEventListener('resize',function(){requestAnimationFrame(function(){document.querySelectorAll('[data-full-room-image]').forEach(fit);});});
})();
