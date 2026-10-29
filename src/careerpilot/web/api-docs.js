const root=document.getElementById('routes');
try {
  const response=await fetch('/openapi.json');if(!response.ok)throw Error('Schema unavailable');
  const schema=await response.json();root.replaceChildren();
  for(const [path,methods] of Object.entries(schema.paths))for(const [method,spec] of Object.entries(methods)){
    if(!['get','post','put','delete','patch'].includes(method))continue;
    const article=document.createElement('article');const title=document.createElement('h3');
    title.textContent=method.toUpperCase()+' '+path;const summary=document.createElement('p');summary.textContent=spec.summary||'';
    article.append(title,summary);root.append(article);
  }
} catch {root.textContent='API schema could not be loaded. Check server readiness.';}
