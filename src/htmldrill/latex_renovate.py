"""latex_renovate — modernize html2latex's output into idiomatic LaTeX.

html2latex is correct but emits *dated* TeX: every paragraph is terminated with an
explicit ``\\par`` (the blank line is the modern idiom), running text is glued
directly onto ``\\end{figure}``, and the preamble is a bare
``\\documentclass{article}`` with no Unicode/font/hyperlink setup. This pass
rewrites that into clean, current LaTeX — WITHOUT changing meaning:

  * body: ``\\par`` terminators → blank-line paragraph breaks; block environments
    (figure/itemize/tabular/…) separated from surrounding prose by blank lines;
    trailing whitespace stripped; runs of blank lines collapsed to one.
  * preamble: ``\\documentclass{article}`` → ``\\documentclass[11pt]{article}`` and
    an engine-adaptive modern package set injected (iftex-guarded inputenc/fontenc
    for pdfTeX; lmodern, microtype, hyperref), each added only if absent so
    html2latex's own auto-detected packages are preserved.

Every transform is idempotent and purely textual — re-running ``renovate`` on its
own output is a no-op. It never removes content, only reshapes whitespace and
augments the preamble.
"""
from __future__ import annotations

import re

#: block environments whose \begin/\end should stand apart from adjacent prose
_BLOCK_ENVS = (
    "figure", "itemize", "enumerate", "tabular", "table", "quote", "quotation",
    "verbatim", "center", "description", "equation", "align",
)
_ENVS_RE = "|".join(_BLOCK_ENVS)


#: settings for the wrapping code listing (verbatim overflows the page on a long
#: single line — a collapsed BibTeX box is exactly that).
_LSTSET = ("\\lstset{basicstyle=\\ttfamily\\small,breaklines=true,"
           "breakatwhitespace=false,columns=fullflexible,keepspaces=true,"
           "showstringspaces=false,frame=single,xleftmargin=0pt}")


def _upgrade_preamble(preamble: str, needs_listings: bool = False) -> str:
    """Modernize the documentclass line and inject a current package set, keeping
    any packages html2latex already emitted."""
    preamble = re.sub(r"\\documentclass(?:\[[^\]]*\])?\{article\}",
                      r"\\documentclass[11pt]{article}", preamble, count=1)

    present: set[str] = set()
    for grp in re.findall(r"\\usepackage(?:\[[^\]]*\])?\{([^}]*)\}", preamble):
        present.update(n.strip() for n in grp.split(","))

    inject: list[str] = ["% --- preamble modernized by htmldrill ---"]
    if "iftex" not in present:
        inject.append("\\usepackage{iftex}")
    # pdfTeX needs explicit UTF-8 + T1; XeTeX/LuaTeX are Unicode-native, so guard it
    inject.append("\\ifpdftex\n  \\usepackage[utf8]{inputenc}\n"
                  "  \\usepackage[T1]{fontenc}\n\\fi")
    for pkg, line in (("lmodern", "\\usepackage{lmodern}"),
                      ("microtype", "\\usepackage{microtype}"),
                      ("hyperref", "\\usepackage[hidelinks]{hyperref}")):
        if pkg not in present:
            inject.append(line)
    if needs_listings and "listings" not in present:
        inject.append("\\usepackage{listings}")
        inject.append(_LSTSET)

    # already modernized? (idempotent) — bail if our marker is present
    if "modernized by htmldrill" in preamble:
        return preamble

    out: list[str] = []
    done = False
    for ln in preamble.splitlines():
        out.append(ln)
        if not done and ln.lstrip().startswith("\\documentclass"):
            out.extend(inject)
            done = True
    if not done:
        out = inject + out
    return "\n".join(out) + "\n"     # always newline-terminate (splitlines drops it)


def _renovate_body(body: str) -> str:
    # 1. \par paragraph terminators → blank-line breaks (not \paragraph/\parbox/…)
    body = re.sub(r"[ \t]*\\par(?![a-zA-Z])[ \t]*", "\n\n", body)
    # 2. unglue a block-env END from text that immediately follows it
    body = re.sub(rf"(\\end\{{(?:{_ENVS_RE})\}})(?=\S)", r"\1\n\n", body)
    # 3. put a break before a block-env BEGIN glued to preceding text
    body = re.sub(rf"(\S)[ \t]*(\\begin\{{(?:{_ENVS_RE})\}})", r"\1\n\n\2", body)
    # 4. strip trailing whitespace, then collapse blank-line runs
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body


#: verbatim (html2latex's <pre> output) OR an already-converted lstlisting
_CODE_RE = re.compile(r"\\begin\{(verbatim|lstlisting)\}(.*?)\\end\{\1\}", re.S)


def renovate(tex: str) -> str:
    """Return a modernized copy of an html2latex document. Idempotent."""
    m = re.search(r"\\begin\{document\}", tex)
    if m:
        preamble, body = tex[:m.start()], tex[m.start():]
    else:                                   # a fragment — body only
        preamble, body = "", tex

    # Protect code/verbatim content from the whitespace transforms (they'd mangle
    # code formatting), then re-emit as a wrapping lstlisting so a long single-line
    # box — a collapsed BibTeX entry — doesn't overflow the page.
    blocks: list[str] = []

    def _stash(mm: "re.Match") -> str:
        blocks.append(mm.group(2).strip("\n"))
        return f"\n\n@@HTMLDRILL_CODE_{len(blocks) - 1}@@\n\n"

    body = _CODE_RE.sub(_stash, body)
    body = _renovate_body(body)
    for i, content in enumerate(blocks):
        listing = "\\begin{lstlisting}\n" + content + "\n\\end{lstlisting}"
        body = body.replace(f"@@HTMLDRILL_CODE_{i}@@", listing)

    if m:
        preamble = _upgrade_preamble(preamble, needs_listings=bool(blocks))
    return (preamble + body).strip() + "\n"
