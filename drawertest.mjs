import { chromium } from 'playwright';
const b=await chromium.launch({executablePath:'/opt/pw-browsers/chromium'});
const pg=await b.newPage(); await pg.goto('file:///home/claude/render5.html');
const r=await pg.evaluate(()=>{
  const t=document.querySelector('table.cbs');
  const toggle=t.querySelector('.drawer-toggle');
  const detail=toggle.closest('tr').nextElementSibling;
  const before=detail.style.display;
  toggle.click();
  const after=detail.style.display;
  const sites=[...detail.querySelectorAll('.sitegrid > div')].map(d=>d.textContent);
  return {beforeDisplay:before, afterDisplay:after, sitesShown:sites};
});
console.log(JSON.stringify(r));
await b.close();
