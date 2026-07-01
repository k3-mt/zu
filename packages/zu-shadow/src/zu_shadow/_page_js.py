"""Shared in-page JavaScript fragments — ONE source of truth for the a11y capture.

The live capture (``live_capture.CAPTURE_JS``), the action-surface enumeration
(``live_executor._ENUMERATE_JS``) and the content projection
(``live_executor._CONTENT_JS``) all resolve a DOM element to its accessibility
``{role, name/label}`` the SAME way — role from the tag/aria, an accessible name
climbing labels/aria/placeholder, a whitespace ``clean``. These fragments were
copy-pasted across the three blobs and had begun to drift (F13). They now live here
once and are composed into each blob, so the action view and the reading view agree
on what a control is called and there is a single place to fix a resolution bug.

These are pure JS *source strings* injected into the page over CDP; there is no
Python behaviour to unit-test here. The Python that USES them
(``_payload_to_raw``/``ax_node_to_target``/``reduce_content``) is the offline-tested
contract. Keeping the fragments identical is the whole point of the de-duplication:
the existing offline tests stay green because the composed blobs are behaviourally
the same JS they were before.
"""

from __future__ import annotations

# Collapse whitespace, trim, cap to 80 chars — the canonical short label form.
_JS_CLEAN = r"""
  function clean(s){ return (s||'').replace(/\s+/g,' ').trim().slice(0,80); }
"""

# Resolve an element's accessibility ROLE from its aria role / tag / input type.
# Locale-independent and selector-free.
_JS_ROLE = r"""
  function role(el){
    const r = el.getAttribute && el.getAttribute('role'); if(r && r.trim()) return r.trim();
    const t = (el.tagName||'').toLowerCase();
    if(t==='button' || t==='summary') return 'button';
    if(t==='a' && el.hasAttribute('href')) return 'link';
    if(t==='input'){const ty=(el.type||'text').toLowerCase();
      if(ty==='submit'||ty==='button'||ty==='image')return'button';
      if(ty==='checkbox')return'checkbox'; if(ty==='radio')return'radio';
      if(ty==='search')return'searchbox'; return'textbox';}
    if(t==='textarea') return 'textbox';
    if(t==='select') return 'combobox';
    if(el.hasAttribute && (el.hasAttribute('onclick')||el.hasAttribute('tabindex'))) return 'button';
    return t || 'generic';
  }
"""

# Resolve an element's accessible NAME: aria-label / aria-labelledby / <label> /
# innerText / value|placeholder|title|alt|name / a labelled icon child, with the
# search-form and submit fallbacks. Depends on ``clean`` being in scope.
_JS_NAME = r"""
  function name(el){
    try{
      const al=el.getAttribute('aria-label'); if(al && al.trim()) return clean(al);
      const lb=el.getAttribute('aria-labelledby');
      if(lb){const n=document.getElementById(lb); const v=n&&clean(n.innerText); if(v) return v;}
      if(el.id){const lab=document.querySelector('label[for="'+CSS.escape(el.id)+'"]');
        const v=lab&&clean(lab.innerText); if(v) return v;}
      const cl=el.closest && el.closest('label'); { const v=cl&&clean(cl.innerText); if(v) return v; }
      const it=clean(el.innerText); if(it) return it;   // innerText skips <style>/<script> CSS soup
      for(const a of ['value','placeholder','title','alt','name']){
        const v=el.getAttribute && el.getAttribute(a); if(v && v.trim()) return clean(v); }
      const ic=el.querySelector && el.querySelector('[aria-label],img[alt],[title]');
      if(ic){ const v=ic.getAttribute('aria-label')||ic.getAttribute('alt')||ic.getAttribute('title');
        if(v && v.trim()) return clean(v); }
      // an unlabeled submit/icon button inside a search form is "Search", else "Submit"
      const ty=(el.type||'').toLowerCase();
      const btn = ty==='submit'||ty==='image'||el.tagName==='BUTTON'||el.getAttribute('role')==='button';
      if(btn){
        const f=el.closest && el.closest('form');
        if(f && (f.getAttribute('role')==='search' || /search/i.test(f.getAttribute('action')||'') ||
                 f.querySelector('[type=search],[name*="search" i],[placeholder*="search" i]'))) return 'Search';
        if(ty==='submit'||ty==='image') return 'Submit';
      }
    }catch(e){}
    return '';
  }
"""

# The shared a11y helpers (clean + role + name), in dependency order. Every in-page
# blob that needs to resolve an element to {role, name} composes this ONE fragment.
A11Y_HELPERS_JS = _JS_CLEAN + _JS_ROLE + _JS_NAME

# The selector for enumerable, actionable affordances — the Action Surface's element
# set. Shared so the capture wiring and the executor's enumeration agree on what an
# actionable control is.
ACTIONABLE_SELECTOR = (
    "button, a[href], [role=button], [role=link], [role=tab], [role=menuitem], "
    "[role=option], [role=checkbox], [role=radio], input, select, textarea, summary, [onclick]"
)
