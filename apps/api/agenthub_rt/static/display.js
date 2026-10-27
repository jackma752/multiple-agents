/* Local presentation only. Model text is never inserted as executable HTML. */
"use strict";
(() => {
  const themeKey = 'agenthub.theme', system = matchMedia('(prefers-color-scheme: light)');
  let preference = 'system';
  try { preference = localStorage.getItem(themeKey) || 'system'; } catch (_) {}
  if (!['system','light','dark'].includes(preference)) preference = 'system';
  function applyTheme() {
    document.documentElement.dataset.theme = preference === 'system' ? (system.matches ? 'light' : 'dark') : preference;
    document.querySelectorAll('[data-theme-picker]').forEach(select => { select.value = preference; });
  }
  function chooseTheme(value) {
    if (!['system','light','dark'].includes(value)) return;
    preference = value;
    try { localStorage.setItem(themeKey, value); } catch (_) {}
    applyTheme();
  }
  applyTheme(); system.addEventListener('change', applyTheme);
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-theme-picker]').forEach(select => select.addEventListener('change', e => chooseTheme(e.target.value)));
    applyTheme();
  });

  const element = (tag, text) => { const node=document.createElement(tag); if (text !== undefined) node.textContent=text; return node; };
  function linkURL(value) {
    try { const url=new URL(value); return ['https:','http:','mailto:'].includes(url.protocol) ? url.href : null; } catch (_) {return null;}
  }
  function inline(parent, source) {
    const token = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|~~[^~\n]+~~|\*[^*\n]+\*|!?\[[^\]\n]*\]\([^\)\n]*\))/g;
    let last=0, match;
    while ((match=token.exec(source))) {
      parent.append(document.createTextNode(source.slice(last,match.index)));
      const s=match[0]; let node;
      if(s[0]==='`') node=element('code',s.slice(1,-1));
      else if(s.startsWith('**')||s.startsWith('__')) node=element('strong',s.slice(2,-2));
      else if(s.startsWith('~~')) node=element('del',s.slice(2,-2));
      else if(s[0]==='*') node=element('em',s.slice(1,-1));
      else {
        const parts=/^!?\[([^\]]*)\]\(([^)]*)\)$/.exec(s), url=linkURL(parts[2].trim());
        if(url) {node=element('a',(s[0]==='!'?'查看图片：':'')+parts[1]);node.href=url;node.target='_blank';node.rel='noopener noreferrer';}
        else node=document.createTextNode(s);
      }
      parent.append(node); last=token.lastIndex;
    }
    parent.append(document.createTextNode(source.slice(last)));
  }

  let serial=0;
  const svgTags=new Set(['svg','g','defs','path','rect','circle','ellipse','line','polyline','polygon','text','tspan','title','desc','linearGradient','radialGradient','stop','marker','use','symbol','clipPath']);
  const svgAttrs=new Set(['id','viewBox','preserveAspectRatio','width','height','x','y','x1','y1','x2','y2','cx','cy','r','rx','ry','d','points','transform','fill','fill-opacity','fill-rule','stroke','stroke-width','stroke-opacity','stroke-linecap','stroke-linejoin','stroke-dasharray','opacity','font-size','font-family','font-weight','text-anchor','dominant-baseline','dx','dy','offset','stop-color','stop-opacity','markerWidth','markerHeight','markerUnits','refX','refY','orient','marker-start','marker-mid','marker-end','gradientUnits','gradientTransform','spreadMethod','clip-path','clipPathUnits','href']);
  function safeSVG(source) {
    if(source.length>500000) throw Error('SVG 太大，请查看原文');
    const doc=new DOMParser().parseFromString(source,'image/svg+xml');
    if(doc.querySelector('parsererror')||doc.documentElement.localName!=='svg') throw Error('SVG 格式不完整');
    const prefix='hub-svg-'+(++serial)+'-', ns='http://www.w3.org/2000/svg'; let count=0;
    function copy(node) {
      if(node.nodeType===Node.TEXT_NODE) return document.createTextNode(node.textContent);
      if(node.nodeType!==Node.ELEMENT_NODE||!svgTags.has(node.localName)||++count>2000) return null;
      const out=document.createElementNS(ns,node.localName);
      for(const attr of node.attributes) {
        const name=attr.localName; let value=attr.value;
        if(!svgAttrs.has(name)||/javascript:|data:|https?:|file:|\/\/|@import|expression/i.test(value)) continue;
        if(name==='href') {if(!/^#[\w.-]+$/.test(value))continue;value='#'+prefix+value.slice(1);}
        if(name==='id') {if(!/^[\w.-]+$/.test(value))continue;value=prefix+value;}
        if(/url\s*\(/i.test(value)) {
          if(!/^url\(#[\w.-]+\)$/.test(value))continue;
          value=value.replace('url(#','url(#'+prefix);
        }
        out.setAttribute(name,value);
      }
      for(const child of node.childNodes) {const clean=copy(child);if(clean)out.append(clean);}
      return out;
    }
    const svg=copy(doc.documentElement);svg.setAttribute('role','img');return svg;
  }
  function codeBlock(source, language) {
    const box=element('div');box.className='code-block';
    if(language==='svg'||language==='xml'&&source.trim().startsWith('<svg')) {
      try {const preview=element('div');preview.className='svg-preview';preview.append(safeSVG(source));box.append(preview);}
      catch(e) {const note=element('p','未渲染图形：'+e.message);note.className='dim';box.append(note);}
    }
    const head=element('div');head.className='code-head';head.append(element('span',language||'代码'));
    const button=element('button','复制代码');button.type='button';button.onclick=async()=>{try{await navigator.clipboard.writeText(source);button.textContent='已复制';}catch(_){button.textContent='请选中复制';}};
    head.append(button);const pre=element('pre');pre.append(element('code',source));
    if(box.childNodes.length) {const details=element('details');details.append(element('summary','查看 SVG 源码'),head,pre);box.append(details);}
    else box.append(head,pre);
    return box;
  }
  function markdown(text) {
    const root=element('div');root.className='markdown';
    if(text.trim().startsWith('<svg')&&text.trim().endsWith('</svg>')) {root.append(codeBlock(text,'svg'));return root;}
    const lines=text.replace(/\r\n?/g,'\n').split('\n'); let i=0;
    const tableCells=line=>line.trim().replace(/^\||\|$/g,'').split(/(?<!\\)\|/).map(x=>x.trim().replace(/\\\|/g,'|'));
    const separator=line=>/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
    while(i<lines.length) {
      const line=lines[i];if(!line.trim()){i++;continue;}
      const fence=/^\s*(`{3,}|~{3,})\s*([\w+-]*)/.exec(line);
      if(fence) {const body=[];i++;while(i<lines.length&&!lines[i].trim().startsWith(fence[1]))body.push(lines[i++]);i++;root.append(codeBlock(body.join('\n'),fence[2].toLowerCase()));continue;}
      const heading=/^(#{1,6})\s+(.+)$/.exec(line);
      if(heading){const h=element('h'+heading[1].length);inline(h,heading[2]);root.append(h);i++;continue;}
      if(i+1<lines.length&&line.includes('|')&&separator(lines[i+1])) {
        const wrap=element('div'),table=element('table'),head=element('thead'),tr=element('tr');wrap.className='table-scroll';
        for(const value of tableCells(line)){const cell=element('th');inline(cell,value);tr.append(cell);}head.append(tr);table.append(head);i+=2;
        const body=element('tbody');while(i<lines.length&&lines[i].trim()&&lines[i].includes('|')){const row=element('tr');for(const value of tableCells(lines[i++])){const cell=element('td');inline(cell,value);row.append(cell);}body.append(row);}
        table.append(body);wrap.append(table);root.append(wrap);continue;
      }
      const list=/^\s*(?:([-+*])|\d+[.)])\s+(.+)$/.exec(line);
      if(list){const ordered=!list[1],node=element(ordered?'ol':'ul');while(i<lines.length){const item=/^\s*(?:([-+*])|\d+[.)])\s+(.+)$/.exec(lines[i]);if(!item||(!item[1])!==ordered)break;const li=element('li');inline(li,item[2]);node.append(li);i++;}root.append(node);continue;}
      if(/^>\s?/.test(line)){const quote=element('blockquote');inline(quote,line.replace(/^>\s?/,''));root.append(quote);i++;continue;}
      if(/^\s*(?:---+|\*\*\*+|___+)\s*$/.test(line)){root.append(element('hr'));i++;continue;}
      const p=element('p');inline(p,line);root.append(p);i++;
    }
    return root;
  }
  window.AppDisplay={markdown,safeSVG,chooseTheme};
})();
