"""
Compact DOM outline for LLMs. Optional: nothing in browser.py depends on it.

*Usage:*
>>> from browser.dom import minimize_dom
>>> outline = await minimize_dom(session.page, sample_repeated=True)
"""

from __future__ import annotations

from patchright.async_api import Page

__all__ = ["minimize_dom"]


async def minimize_dom(
    page: Page,
    *,
    sample_repeated: bool = False,
    max_depth: int = 18,
    max_text: int = 150,
    max_attr: int = 200,
    max_classes: int = 6,
    max_lines: int = 1500,
    keep_hidden: bool = False,
    collapse_wrappers: bool = True,
) -> str:
    """
    A compact, indented outline of <body> for an LLM to read, built in one
    evaluate() call. It keeps ARIA and selector-friendly attributes and live
    form values, and drops scripts, styles and hidden elements. Password and
    file input values are never included, but it is not a general redaction
    tool. sample_repeated collapses long runs of identical siblings (form
    fields are never collapsed).

    *Usage:*
    >>> outline = await minimize_dom(session.page, sample_repeated=True)
    """
    return await page.locator("body").evaluate(_MINIMIZE_DOM_JS, {
        "sampleRepeated": sample_repeated,
        "maxDepth": max_depth,
        "maxText": max_text,
        "maxAttr": max_attr,
        "maxClasses": max_classes,
        "maxLines": max_lines,
        "keepHidden": keep_hidden,
        "collapseWrappers": collapse_wrappers,
    })


_MINIMIZE_DOM_JS = r"""
(root, o) => {
    // Compared against localName: SVG elements report lowercase tagNames.
    const IGNORED = new Set([
        "script", "style", "noscript", "template", "link", "meta",
        "svg", "canvas", "br", "hr", "wbr", "source", "track", "col", "colgroup",
    ]);

    // Attribute-less pass-through containers that are safe to flatten.
    const WRAPPERS = new Set(["div", "span"]);

    // Formatting tags whose text is merged into the parent's text line.
    const INLINE = new Set(["b", "i", "em", "strong", "u", "s", "small", "mark", "sub", "sup", "code"]);

    const KEEP = new Set([
        "id", "class", "href", "src", "title", "alt", "role", "name", "type",
        "value", "placeholder", "for", "itemprop",
        "disabled", "checked", "selected", "readonly", "required", "multiple",
        "contenteditable", "target", "action", "method",
    ]);
    const BOOL = new Set([
        "disabled", "checked", "selected", "readonly", "required", "multiple",
        "contenteditable",
    ]);

    // data-* is mostly framework noise; keep only the selector-friendly ones.
    const DATA_KEEP = /^data-(testid|test-id|test|qa|cy|automation-id|id|action|role|name|type|state)$/;

    // Generated class names (CSS-in-JS / CSS modules) carry no meaning.
    const GENERATED = /^(css|sc|jsx|svelte|emotion|styled)-[\w-]{4,}$|^_[\w-]{5,}$/;

    // State classes that should not break up a run of "identical" siblings.
    const STATE = /(^|[-_])(active|selected|current|open|disabled|checked|focused?|hover|first|last|odd|even)$/;

    const EDITABLE = "input:not([type=hidden]), select, textarea, [contenteditable=\"\"], [contenteditable=true]";

    const lines = [];
    let truncated = false;

    const squash = s => s.replace(/\s+/g, " ").trim();
    const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

    const hiddenCache = new WeakMap();
    function isHidden(el) {
        if (o.keepHidden) return false;
        let hidden = hiddenCache.get(el);
        if (hidden === undefined) {
            hidden = el.hidden === true ||
                (el.localName === "input" && el.type === "hidden") ||
                getComputedStyle(el).display === "none";
            hiddenCache.set(el, hidden);
        }
        return hidden;
    }

    const inlinable = c =>
        INLINE.has(c.localName) &&
        c.children.length === 0 &&
        [...c.attributes].every(a => a.name === "class" || a.name === "style");

    // Light DOM children plus open shadow-root children.
    const kidsOf = el =>
        [...(el.shadowRoot ? el.shadowRoot.children : []), ...el.children]
            .filter(c => !IGNORED.has(c.localName) && !inlinable(c) && !isHidden(c));

    function ownText(el) {
        let s = "";
        for (const n of el.childNodes) {
            if (n.nodeType === Node.TEXT_NODE) {
                s += n.textContent;
            } else if (n.nodeType === Node.ELEMENT_NODE && inlinable(n) && !isHidden(n)) {
                s += n.textContent;
            }
        }
        return clip(squash(s), o.maxText);
    }

    function shorten(name, v) {
        if ((name === "src" || name === "href") && /^data:/i.test(v))
            return v.slice(0, 24) + "…";
        return clip(v, o.maxAttr);
    }

    function attrString(el) {
        const tag = el.localName;
        const attrs = new Map();

        for (const a of el.attributes) {
            const n = a.name;
            if (n === "class") {
                const cls = a.value.split(/\s+/)
                    .filter(t => t && !GENERATED.test(t))
                    .slice(0, o.maxClasses)
                    .join(" ");
                if (cls) attrs.set(n, cls);
            } else if (KEEP.has(n) || n.startsWith("aria-") || DATA_KEEP.test(n)) {
                attrs.set(n, a.value);
            }
        }

        // Attributes only hold the initial HTML; read the live state instead.
        if (tag === "input") {
            if (el.type === "checkbox" || el.type === "radio") {
                if (el.checked) attrs.set("checked", "");
                else attrs.delete("checked");
            } else if (el.type === "password" || el.type === "file") {
                attrs.delete("value");            // never leak secrets
            } else if (el.value) {
                attrs.set("value", el.value);
            } else {
                attrs.delete("value");
            }
        } else if (tag === "textarea" || tag === "select") {
            if (el.value) attrs.set("value", el.value);
            else attrs.delete("value");
        } else if (tag === "option") {
            if (el.selected) attrs.set("selected", "");
            else attrs.delete("selected");
        }

        const out = [];
        for (const [n, v] of attrs) {
            if (BOOL.has(n) && (v === "" || v === n || v === "true")) out.push(n);
            else if (v !== "") out.push(`${n}=${JSON.stringify(shorten(n, v))}`);
        }
        return out.join(" ");
    }

    // Tag + class (minus state classes) for the node and its first children,
    // so one "active" item doesn't split a run of otherwise identical items.
    const ident = el =>
        el.localName + "." +
        (el.getAttribute("class") || "")
            .split(/\s+/).filter(t => t && !STATE.test(t)).sort().join(".");

    const sig = el =>
        ident(el) + "|" + [...el.children].slice(0, 6).map(ident).join(",");

    // Rows like <div class="form-group"> look identical but each holds a different input.
    const hasFields = el => el.matches(EDITABLE) || !!el.querySelector(EDITABLE);

    function walkChildren(kids, depth) {
        const indent = "  ".repeat(depth);
        const sigs = kids.map(sig);

        for (let i = 0; i < kids.length;) {
            let j = i + 1;
            while (j < kids.length && sigs[j] === sigs[i]) j++;

            const count = j - i;
            const size = kids[i].querySelectorAll("*").length;
            // Complex items collapse from 4 repeats; trivial ones only when huge.
            const collapse =
                o.sampleRepeated &&
                !hasFields(kids[i]) &&
                ((count >= 4 && size >= 5) || count >= 25);

            if (collapse) {
                const cls = k => kids[k].getAttribute("class") || "";
                let skipped = 0;
                const flush = () => {
                    if (skipped) lines.push(`${indent}[... ${skipped} omitted ...]`);
                    skipped = 0;
                };

                lines.push(`${indent}[REPEATED ${count} TIMES]`);
                walk(kids[i], depth);
                walk(kids[i + 1], depth);
                for (let k = i + 2; k < j - 1; k++) {
                    // Items that differ in class (active/selected/...) stay visible.
                    if (cls(k) !== cls(i) ||
                        kids[k].matches("[aria-selected=true],[aria-current],[aria-checked=true]")) {
                        flush();
                        walk(kids[k], depth);
                    } else {
                        skipped++;
                    }
                }
                flush();
                walk(kids[j - 1], depth);
            } else {
                for (let k = i; k < j; k++) walk(kids[k], depth);
            }
            i = j;
        }
    }

    function walk(el, depth) {
        if (lines.length >= o.maxLines) { truncated = true; return; }

        const tag = el.localName;
        const kids = kidsOf(el);
        const attrs = attrString(el);
        const text = tag === "textarea" ? "" : ownText(el);

        // Flatten <div><div><div>…</div></div></div> chains and drop empty ones.
        if (o.collapseWrappers && WRAPPERS.has(tag) && !attrs && !text) {
            if (kids.length === 0) return;
            if (kids.length === 1) return walk(kids[0], depth);
        }

        const indent = "  ".repeat(depth);
        lines.push(`${indent}<${tag}${attrs ? " " + attrs : ""}>` + (text ? ` ${text}` : ""));

        if (!kids.length) return;
        if (depth >= o.maxDepth) {
            lines.push(`${indent}  [... ${kids.length} children not shown (depth limit) ...]`);
            return;
        }
        walkChildren(kids, depth + 1);
    }

    walk(root, 0);
    if (truncated) lines.push(`[... output truncated at ${o.maxLines} lines ...]`);
    return lines.join("\n");
}
"""
