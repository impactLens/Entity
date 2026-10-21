#!/usr/bin/env python3
"""Shared NER core (spaCy + NLTK + lexicons + regex). Imported by app.py."""
import argparse
import json
import re
import sys

# Lower number wins when spans overlap.
PRIORITY = {"SECURITY": 0, "GEOPOLITICAL": 1, "ACADEMIC": 2, "DATE": 3,
            "TIME": 3, "COUNTRY": 4, "INSTITUTION": 5, "PERSON": 6,
            "LOCATION": 7, "PHRASE": 8}

LEXICON = {
    "SECURITY": [
        "cybersecurity", "cyber security", "cyber attack", "cyberattack",
        "ransomware", "malware", "phishing", "zero-day", "encryption",
        "surveillance", "espionage", "intelligence agency", "national security",
        "counterterrorism", "counter-terrorism", "terrorism", "terrorist",
        "nuclear deterrence", "deterrence", "threat actor", "vulnerability",
        "firewall", "data breach", "insider threat", "incident response",
        "penetration testing", "armed forces", "military", "missile",
        "border security", "disinformation", "cyber warfare", "sabotage",
        "intrusion detection", "threat model", "homeland security",
    ],
    "GEOPOLITICAL": [
        "sanctions", "diplomacy", "sovereignty", "geopolitics", "geopolitical",
        "alliance", "treaty", "summit", "trade war", "superpower",
        "balance of power", "soft power", "hard power", "cold war",
        "non-alignment", "multilateralism", "annexation", "proxy war",
        "regime change", "embassy", "foreign policy", "global south",
        "great power", "spheres of influence", "sphere of influence",
        "territorial dispute", "ceasefire", "peacekeeping", "world order",
        "belt and road", "nation-state", "self-determination",
    ],
    "ACADEMIC": [
        "hypothesis", "methodology", "literature review", "peer review",
        "peer-reviewed", "theoretical framework", "empirical", "qualitative",
        "quantitative", "epistemology", "ontology", "paradigm", "thesis",
        "dissertation", "meta-analysis", "corpus", "regression",
        "citation", "discourse analysis", "case study", "longitudinal",
        "dataset", "statistical significance", "research question",
        "conceptual framework", "ethnography", "sampling", "variable",
    ],
}

COUNTRY_FALLBACK = {
    "united states", "united kingdom", "china", "russia", "india", "japan",
    "germany", "france", "italy", "canada", "brazil", "australia", "mexico",
    "spain", "indonesia", "turkey", "saudi arabia", "iran", "iraq", "israel",
    "egypt", "south africa", "nigeria", "ukraine", "poland", "pakistan",
    "bangladesh", "vietnam", "thailand", "south korea", "north korea",
    "argentina", "colombia", "singapore", "malaysia", "philippines",
    "netherlands", "sweden", "norway", "finland", "denmark", "switzerland",
    "afghanistan", "syria", "yemen", "lebanon", "taiwan", "ethiopia", "kenya",
}
COUNTRY_ALIASES = {"usa", "us", "u.s.", "u.s.a.", "uk", "u.k.", "uae",
                   "russia", "south korea", "north korea", "vietnam", "iran",
                   "syria", "turkey", "taiwan", "the netherlands"}


def load_countries():
    names = set(COUNTRY_FALLBACK) | COUNTRY_ALIASES
    try:
        import pycountry
        for c in pycountry.countries:
            for attr in ("name", "common_name", "official_name"):
                v = getattr(c, attr, None)
                if v:
                    names.add(v.lower())
    except ImportError:
        pass
    return names


COUNTRIES = load_countries()


def is_country(text):
    t = re.sub(r"^the\s+", "", text.strip().lower())
    return t in COUNTRIES or ("the " + t) in COUNTRIES


def span(start, end, label, text, source):
    return {"start": start, "end": end, "label": label, "text": text,
            "source": source}


# ---------- lexicon matching ----------
def compile_lexicon(lexicon):
    """Return [(label, compiled_regex)]. Compile once and reuse."""
    compiled = []
    for label, terms in lexicon.items():
        terms = sorted(set(terms), key=len, reverse=True)
        if not terms:
            continue
        alts = []
        for t in terms:
            parts = [re.escape(p) for p in re.split(r"[\s-]+", t.strip()) if p]
            alts.append(r"[\s-]+".join(parts) + r"(?:s|es)?")
        compiled.append((label, re.compile(
            r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)", re.I)))
    return compiled


def lexicon_spans(text, lexicon, compiled=None):
    out = []
    for label, rx in (compiled or compile_lexicon(lexicon)):
        for m in rx.finditer(text):
            out.append(span(m.start(), m.end(), label, m.group(0), "lexicon"))
    return out


# ---------- date / time regex ----------
_MON = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|"
        r"July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)")
_DAY = r"(?:Mon|Tues?|Wed(?:nes)?|Thu(?:rs?)?|Fri|Sat(?:ur)?|Sun)(?:day)?"
_ORD = r"\d{1,2}(?:st|nd|rd|th)?"
DATE_RX = [
    r"\b\d{4}-\d{2}-\d{2}\b",
    r"\b\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\b",
    rf"\b{_MON}\.?\s+{_ORD}(?:,?\s+\d{{4}})?\b",
    rf"\b{_ORD}\s+(?:of\s+)?{_MON}\.?(?:,?\s+\d{{4}})?\b",
    rf"\b{_MON}\.?,?\s+\d{{4}}\b",
    rf"\b{_DAY}\b",
    r"\b(?:Q[1-4]|H[12])\s+\d{4}\b",
]
TIME_RX = [
    r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\s?(?:[ap]\.m\.|[ap]m\b))?"
    r"(?:\s?(?:UTC|GMT|[A-Z]{2,4}T)\b)?",
    r"\b\d{1,2}\s?(?:[ap]\.m\.|[ap]m\b)",
    r"\b(?:noon|midnight)\b",
]


def datetime_spans(text):
    out = []
    for label, pats in (("DATE", DATE_RX), ("TIME", TIME_RX)):
        for pat in pats:
            for m in re.finditer(pat, text, re.I):
                out.append(span(m.start(), m.end(), label, m.group(0),
                                "regex"))
    return out


# ---------- spaCy ----------
def doc_spans(doc):
    """Map a spaCy Doc's entities to our labels."""
    out = []
    for e in doc.ents:
        k = e.label_
        if k == "PERSON":
            lab = "PERSON"
        elif k == "GPE":
            lab = "COUNTRY" if is_country(e.text) else "LOCATION"
        elif k in ("LOC", "FAC"):
            lab = "LOCATION"
        elif k == "ORG":
            lab = "INSTITUTION"
        elif k == "NORP":
            lab = "GEOPOLITICAL"
        elif k in ("DATE", "TIME"):
            lab = k
        else:
            continue
        out.append(span(e.start_char, e.end_char, lab, e.text, "spacy"))
    return out


def spacy_spans(text, model):
    try:
        import spacy
    except ImportError:
        print("spaCy not installed; skipping", file=sys.stderr)
        return []
    nlp = None
    for name in ([model] if model else ["en_core_web_trf", "en_core_web_sm"]):
        try:
            nlp = spacy.load(name)
            break
        except OSError:
            continue
    if nlp is None:
        print("No spaCy model found; run: python -m spacy download "
              "en_core_web_sm", file=sys.stderr)
        return []
    nlp.max_length = max(nlp.max_length, len(text) + 1000)
    return doc_spans(nlp(text))


# ---------- NLTK ----------
def ensure_nltk():
    import nltk
    need = [("tokenizers/punkt_tab", "punkt_tab"),
            ("taggers/averaged_perceptron_tagger_eng",
             "averaged_perceptron_tagger_eng"),
            ("chunkers/maxent_ne_chunker_tab", "maxent_ne_chunker_tab"),
            ("corpora/words", "words"),
            ("corpora/stopwords", "stopwords")]
    for path, pkg in need:
        try:
            nltk.data.find(path)
        except LookupError:
            nltk.download(pkg, quiet=True)


NLTK_MAP = {"PERSON": "PERSON", "ORGANIZATION": "INSTITUTION",
            "LOCATION": "LOCATION", "FACILITY": "LOCATION",
            "GSP": "LOCATION", "GPE": "GPE"}


_NLTK_CACHE = {}


def _nltk_resources():
    """Build tokenizers/parsers once per process."""
    if not _NLTK_CACHE:
        import nltk
        from nltk.corpus import stopwords
        from nltk.tokenize import NLTKWordTokenizer
        try:
            from nltk.tokenize.punkt import PunktTokenizer
            sent = PunktTokenizer("english")      # pretrained: knows "Dr.", "U.S."
        except Exception:
            from nltk.tokenize import PunktSentenceTokenizer
            sent = PunktSentenceTokenizer()
        _NLTK_CACHE["res"] = (set(stopwords.words("english")),
                              NLTKWordTokenizer(), sent,
                              nltk.RegexpParser("NP: {<JJ.*>*<NN.*>+}"))
    return _NLTK_CACHE["res"]


def _tag(tokens):
    """POS-tag with one shared tagger instance."""
    try:
        if "tagger" not in _NLTK_CACHE:
            from nltk.tag.perceptron import PerceptronTagger
            _NLTK_CACHE["tagger"] = PerceptronTagger()
        return _NLTK_CACHE["tagger"].tag(tokens)
    except Exception:
        import nltk
        return nltk.pos_tag(tokens)


def nltk_spans(text, phrases=True):
    try:
        import nltk
        from nltk.corpus import stopwords
        from nltk.tokenize import NLTKWordTokenizer, PunktSentenceTokenizer
        ensure_nltk()
    except Exception as exc:  # missing package or no network for data
        print(f"NLTK unavailable ({exc}); skipping", file=sys.stderr)
        return []
    stop, wt, st, np_parser = _nltk_resources()
    out = []
    for s0, s1 in st.span_tokenize(text):
        sent = text[s0:s1]
        spans = [(a + s0, b + s0) for a, b in wt.span_tokenize(sent)]
        if not spans:
            continue
        toks = [text[a:b] for a, b in spans]
        tagged = _tag(toks)

        i = 0
        for node in nltk.ne_chunk(tagged):
            if isinstance(node, nltk.Tree):
                n = len(node)
                a, b = spans[i][0], spans[i + n - 1][1]
                i += n
                lab = NLTK_MAP.get(node.label())
                if lab == "GPE":
                    lab = "COUNTRY" if is_country(text[a:b]) else "LOCATION"
                if lab:
                    out.append(span(a, b, lab, text[a:b], "nltk"))
            else:
                i += 1

        if phrases:
            i = 0
            for node in np_parser.parse(tagged):
                if isinstance(node, nltk.Tree):
                    n = len(node)
                    a, b = spans[i][0], spans[i + n - 1][1]
                    words = [w.lower() for w, _ in node.leaves()]
                    i += n
                    if n >= 2 and not all(w in stop for w in words):
                        out.append(span(a, b, "PHRASE", text[a:b], "nltk"))
                else:
                    i += 1
    return out


# ---------- merge ----------
def resolve(spans):
    """Drop overlaps: lower PRIORITY wins, then longer span. O(n log n)."""
    import bisect
    kept, starts = [], []
    for s in sorted(spans, key=lambda s: (PRIORITY[s["label"]],
                                          -(s["end"] - s["start"]), s["start"])):
        i = bisect.bisect_left(starts, s["start"])
        if i > 0 and kept[i - 1]["end"] > s["start"]:
            continue
        if i < len(kept) and kept[i]["start"] < s["end"]:
            continue
        starts.insert(i, s["start"])
        kept.insert(i, s)
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="file path or - for stdin")
    ap.add_argument("--model", default=None)
    ap.add_argument("--lexicon", help="JSON {LABEL: [terms]} to extend lists")
    ap.add_argument("--no-spacy", action="store_true")
    ap.add_argument("--no-nltk", action="store_true")
    ap.add_argument("--no-phrases", action="store_true")
    ap.add_argument("--format", choices=["json", "table"], default="json")
    a = ap.parse_args()

    if a.path == "-":
        sys.stdin.reconfigure(encoding="utf-8", newline="")
        text = sys.stdin.read()
    else:
        with open(a.path, encoding="utf-8", newline="") as f:
            text = f.read()

    lex = {k: list(v) for k, v in LEXICON.items()}
    if a.lexicon:
        with open(a.lexicon, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                lex.setdefault(k.upper(), []).extend(v)

    spans = lexicon_spans(text, lex) + datetime_spans(text)
    if not a.no_spacy:
        spans += spacy_spans(text, a.model)
    if not a.no_nltk:
        spans += nltk_spans(text, phrases=not a.no_phrases)
    result = resolve(spans)

    if a.format == "table":
        for s in result:
            print(f"{s['start']:>7} {s['end']:>7}  {s['label']:<13} "
                  f"{s['source']:<8} {s['text']!r}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        json.dump(result, sys.stdout, ensure_ascii=True)



_SENT_BREAK = re.compile(r"(?<=[.!?])\s+")


def _best_cut(window, min_cut):
    """End index for a chunk: paragraph break, else sentence end, else space."""
    i = window.rfind("\n\n")
    if i >= min_cut:
        return i + 2
    last = None
    for m in _SENT_BREAK.finditer(window):
        last = m
    if last and last.end() >= min_cut:
        return last.end()
    i = max(window.rfind(" "), window.rfind("\n"))
    if i >= min_cut:
        return i + 1
    return len(window)


def chunk_text(text, max_chars=5000):
    """Split long text into [(offset, chunk)] on natural boundaries.
    Chunks tile the text exactly: ''.join(c for _, c in chunks) == text."""
    if len(text) <= max_chars:
        return [(0, text)]
    chunks, pos = [], 0
    while pos < len(text):
        window = text[pos:pos + max_chars]
        last = pos + max_chars >= len(text)
        cut = len(window) if last else _best_cut(window, max_chars // 2)
        chunks.append((pos, text[pos:pos + cut]))
        pos += cut
    return chunks


def shift(spans, offset):
    for s in spans:
        s["start"] += offset
        s["end"] += offset
    return spans


def to_utf16_offsets(text, spans):
    """Convert code-point offsets (Python) to UTF-16 units (JavaScript)."""
    if all(ord(c) <= 0xFFFF for c in text):
        return spans
    cum = [0] * (len(text) + 1)
    for i, c in enumerate(text):
        cum[i + 1] = cum[i] + (2 if ord(c) > 0xFFFF else 1)
    for s in spans:
        s["start"], s["end"] = cum[s["start"]], cum[s["end"]]
    return spans


if __name__ == "__main__":
    main()
