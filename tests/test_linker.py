import json
import re

import pytest

from linker import content, scoring, validate
from linker.main import process_post, ready_rows
from linker.sheets import parse_tab
from linker.text import norm_url

from tests.fixtures import BASE, DRAFT_RAW, FakeControl, FakeWP, cfg, inventory

SETTINGS_SKIP = ["faq", "frequently asked questions"]


# ---------- MasterSheet parsing ----------

OCT_HEADER = ["Course", "Cluster", "Topic", "Primary Keyword", "SV", "Author", "Final Content Doc",
              "Content Status", "Publishing Status", "Publishing Date", "Draft Link", "Live Link",
              "WP Author", "WP Category", "WP Category 2"]


def test_parse_tab_reads_draft_id_and_categories():
    c = cfg()
    rows = parse_tab("Oct'26", [OCT_HEADER, [
        "IIT Delhi · AI FDE", "FDE skills", "LLM Evals for FDEs", "llm evaluation", "500", "Purnima", "O29",
        "Ready to Publish", "Yet to Publish", "",
        "https://www.interviewbit.com/varsity/blog/wp-admin/post.php?post=1940&action=edit", "",
        "Varsity", "Forward Deployed Engineering", ""]], c.get("mastersheet", "columns"))
    assert rows[0].post_id == 1940
    assert rows[0].wp_categories == ["Forward Deployed Engineering"]
    assert rows[0].primary_keyword == "llm evaluation"


def test_ready_rows_skips_published_done_and_missing_draft():
    c = cfg()
    cols = c.get("mastersheet", "columns")
    link = "https://x/wp-admin/post.php?post={}&action=edit"
    grid = [OCT_HEADER,
            ["c", "c", "A", "a", "", "", "", "Ready to Publish", "Yet to Publish", "", link.format(1), "", "", "FDE", ""],
            ["c", "c", "B", "b", "", "", "", "Ready to Publish", "Published", "", link.format(2), "", "", "FDE", ""],
            ["c", "c", "C", "c", "", "", "", "Ready to Publish", "Yet to Publish", "", link.format(3), "", "", "FDE", ""],
            ["c", "c", "D", "d", "", "", "", "Ready to Publish", "Yet to Publish", "", "", "", "", "FDE", ""]]
    rows = parse_tab("Oct'26", grid, cols)
    ids = [r.post_id for r in ready_rows(rows, c.settings, done={3}, only=None)]
    assert ids == [1]


# ---------- draft parsing ----------

def test_parse_skips_intro_faq_and_formatted_sentences():
    p = content.parse(DRAFT_RAW, SETTINGS_SKIP, skip_first=1)
    texts = [s.text for s in p.sentences.values()]
    assert not any(t.startswith("Forward deployed engineers sit") for t in texts)   # intro
    assert not any("Is prompt injection a real risk" in t for t in texts)            # FAQ
    assert not any("observability" in t and "saves the week" in t for t in texts)    # has <strong>
    assert any(t.startswith("Tracing every call") for t in texts)                     # plain sibling ok


def test_apply_edit_inserts_anchor_and_keeps_blocks():
    p = content.parse(DRAFT_RAW, SETTINGS_SKIP)
    sid = next(s.id for s in p.sentences.values() if s.text.startswith("Prompt injection is one"))
    new = content.apply_edits(p, [{
        "sentence_id": sid, "anchor": "prompt injection attacks",
        "rewritten": "Defending against prompt injection attacks is one of the risks that forward deployed engineers handle on every customer project they take on.",
        "url": "https://www.interviewbit.com/varsity/blog/prompt-injection/"}])
    assert '<a href="https://www.interviewbit.com/varsity/blog/prompt-injection/">prompt injection attacks</a>' in new
    assert new.count("<!-- wp:paragraph -->") == DRAFT_RAW.count("<!-- wp:paragraph -->")
    assert "<strong>observability</strong>" in new


# ---------- scoring ----------

def _draft(p):
    return scoring.Draft(post_id=99, title="LLM Evals for FDEs", categories=["Forward Deployed Engineering"],
                         primary_keyword="llm evaluation", sentences=[s.text for s in p.sentences.values()],
                         word_count=900, existing_links=p.existing_links)


def test_scores_prefer_program_and_same_category():
    c = cfg()
    p = content.parse(DRAFT_RAW, SETTINGS_SKIP)
    cands = scoring.score_targets(inventory(), _draft(p), c)
    by_url = {x.target.url: x for x in cands}
    fde_prog = by_url["https://www.example.com/programs/fde/"]
    assert fde_prog.cat_match == "same"
    assert by_url["https://www.interviewbit.com/varsity/blog/prompt-injection/"].relevance == 1.0
    assert "https://www.interviewbit.com/varsity/blog/takt-time/" not in by_url   # irrelevant, dropped
    assert fde_prog.score > by_url["https://www.interviewbit.com/varsity/blog/rlhf/"].score if \
        "https://www.interviewbit.com/varsity/blog/rlhf/" in by_url else True


def test_plan_requires_own_program_and_meets_category_share():
    c = cfg()
    p = content.parse(DRAFT_RAW, SETTINGS_SKIP)
    n, shortlist = scoring.plan(scoring.score_targets(inventory(), _draft(p), c), _draft(p), c)
    assert n == 3                                      # 900 words / 275 -> 3
    assert shortlist[0].target.program_id == "fde" and shortlist[0].required
    planned_blogs = [x for x in shortlist[:n] if x.target.type == "blog"]
    assert all(x.cat_match == "same" for x in planned_blogs)


def test_cannibalising_target_is_excluded():
    c = cfg()
    p = content.parse(DRAFT_RAW, SETTINGS_SKIP)
    d = _draft(p)
    d.primary_keyword = "prompt injection"
    urls = {x.target.url for x in scoring.score_targets(inventory(), d, c)}
    assert "https://www.interviewbit.com/varsity/blog/prompt-injection/" not in urls


# ---------- validation ----------

def _ids_and_parsed():
    c = cfg()
    p = content.parse(DRAFT_RAW, SETTINGS_SKIP)
    n, shortlist = scoring.plan(scoring.score_targets(inventory(), _draft(p), c), _draft(p), c)
    ids = {f"T{i}": x for i, x in enumerate(shortlist, 1)}
    return c, p, n, ids


def test_validation_rejects_bad_edits():
    c, p, n, ids = _ids_and_parsed()
    sid = next(s.id for s in p.sentences.values() if s.text.startswith("Prompt injection is one"))
    orig = p.sentences[sid].text
    edits = [
        {"sentence_id": "p99.s0", "target_id": "T1", "anchor": "FDE course", "rewritten": "x FDE course"},
        {"sentence_id": sid, "target_id": "T404", "anchor": "prompt injection", "rewritten": orig},
        {"sentence_id": sid, "target_id": "T1", "anchor": "click here", "rewritten": orig + " click here"},
        {"sentence_id": sid, "target_id": "T1", "anchor": "FDE course",
         "rewritten": "Prompt injection is one of the 3 risks that forward deployed engineers have to defend "
                      "against on every customer project they take on, a core FDE course topic."},
        {"sentence_id": sid, "target_id": "T1", "anchor": "FDE course",
         "rewritten": "Teams that took an FDE course say defending systems is hard."},
    ]
    res = validate.check(edits, ids, p, n, c.settings)
    reasons = [r for _, r in res.rejected]
    assert reasons[:3] == ["unknown sentence_id", "unknown target_id", "generic anchor"]
    assert reasons[3] in ("rewrite introduced new numbers", "program link changed the writer's words "
                          "(only inserting a short phrase is allowed)",
                          "program links must use the writer's own words (no inserted phrase)")
    assert reasons[4] == "rewrite changed too much" or reasons[4].startswith(("rewrite adds words", "program link"))
    assert not res.accepted and "required program link not placed" in res.notes


# ---------- end to end (fake WP + fake LLM) ----------

class FakeLLM:
    def __init__(self):
        self.prompt = None

    def propose(self, prompt):
        self.prompt = prompt
        ids = dict(re.findall(r"^(T\d+) \| \w+ \| ([^|]+) \|", prompt, re.M))
        tid = {v.strip(): k for k, v in ids.items()}
        sid_pi = re.search(r"^(p\d+\.s\d+): Prompt injection is one", prompt, re.M).group(1)
        sid_tr = re.search(r"^(p\d+\.s\d+): Many engineers build", prompt, re.M).group(1)
        return [
            {"sentence_id": sid_pi, "target_id": tid["Prompt Injection Attacks"], "anchor": "Prompt injection",
             "rewritten": "Prompt injection is one of the risks that forward deployed engineers have to defend against on every customer project they take on."},
            {"sentence_id": sid_tr, "target_id": tid["IIT Delhi - AI Forward Deployed Engineering"],
             "anchor": "forward deployed engineering programme",
             "rewritten": "Many engineers build these skills through a forward deployed engineering programme rather than on the job alone, because the feedback loop is faster."},
        ]


class Row:
    post_id, primary_keyword, wp_categories, topic = 1940, "llm evaluation", ["Forward Deployed Engineering"], "LLM Evals"


@pytest.mark.parametrize("dry_run", [True, False])
def test_process_post_end_to_end(dry_run):
    c = cfg()
    wp, ctl, llm = FakeWP(DRAFT_RAW), FakeControl(), FakeLLM()
    res = process_post(Row(), wp, inventory(), {7: "Forward Deployed Engineering"}, llm, c, ctl, dry_run)
    assert res["status"] == "linked" and len(res["accepted"]) == 2
    assert "REQUIRED" in llm.prompt and "Is prompt injection a real risk" not in llm.prompt
    assert len(ctl.changes) == 2
    if dry_run:
        assert wp.updated is None
    else:
        assert 'href="https://www.example.com/programs/fde/"' in wp.updated


def test_published_post_is_never_edited():
    c = cfg()
    wp, ctl = FakeWP(DRAFT_RAW, status="publish"), FakeControl()
    res = process_post(Row(), wp, inventory(), {}, FakeLLM(), c, ctl, dry_run=False)
    assert res["status"] == "skipped" and wp.updated is None


def test_norm_url():
    assert norm_url("http://WWW.interviewbit.com/varsity/blog/x?utm=1#y") == "https://www.interviewbit.com/varsity/blog/x/"


# ---------- link hygiene: writer-added links ----------

HYGIENE_RAW = """<!-- wp:paragraph -->
<p>Intro paragraph that is always skipped by the linker for safety reasons here.</p>
<!-- /wp:paragraph -->

<!-- wp:paragraph -->
<p>Teams evaluate models before shipping them to customers in production. Read the <a href="/varsity/blog/llm-evals/">evaluation checklist</a> before you start building anything new. Retrieval augmented generation is usually the first architecture teams reach for.</p>
<!-- /wp:paragraph -->

<!-- wp:paragraph -->
<p>Prompt injection is one of the risks that forward deployed engineers defend against daily. Guardrails reduce the blast radius when a model is tricked by hostile input. See <a href="https://owasp.org/llm-top-10/">OWASP guidance</a> for the full list of risks.</p>
<!-- /wp:paragraph -->

<!-- wp:paragraph -->
<p>Many engineers build these skills through a forward deployed engineering programme rather than on the job alone, because the feedback loop is faster.</p>
<!-- /wp:paragraph -->
"""


def _hygiene():
    c = cfg()
    p = content.parse(HYGIENE_RAW, SETTINGS_SKIP, site_base=BASE,
                      internal_prefixes=["https://www.interviewbit.com/varsity/",
                                         "https://www.example.com/programs/"])
    return c, p


def test_existing_links_are_classified_and_resolved():
    _, p = _hygiene()
    assert p.existing_internal == 1 and p.existing_external == 1
    assert "https://www.interviewbit.com/varsity/blog/llm-evals/" in p.existing_links   # relative href resolved


def test_no_new_link_in_or_next_to_a_linked_sentence():
    c, p = _hygiene()
    ok = content.eligible_sentences(p, gap=1, max_total_per_para=2)
    texts = [s.text for s in ok.values()]
    assert not any(t.startswith("Teams evaluate") for t in texts)        # right before a link
    assert not any(t.startswith("Retrieval augmented") for t in texts)   # right after a link
    assert not any(t.startswith("Guardrails reduce") for t in texts)     # right before external link
    assert any(t.startswith("Prompt injection is one") for t in texts)   # two sentences away: fine
    assert any(t.startswith("Many engineers build") for t in texts)


def test_existing_internal_links_count_toward_budget():
    c, p = _hygiene()
    d = scoring.Draft(post_id=1, title="t", categories=["Forward Deployed Engineering"], primary_keyword="",
                      sentences=[], word_count=900, existing_links=p.existing_links,
                      existing_internal=2, existing_external=5)
    assert scoring.new_link_budget(d, c) == 1          # 3 total - 2 already present; externals ignored
    c.settings["linking"]["count_external_in_budget"] = True
    assert scoring.new_link_budget(d, c) == 0


def test_existing_program_link_satisfies_program_quota():
    c = cfg()
    p = content.parse(DRAFT_RAW, SETTINGS_SKIP)
    d = _draft(p)
    d.existing_program = 1
    n, shortlist = scoring.plan(scoring.score_targets(inventory(), d, c), d, c)
    assert not any(x.required for x in shortlist)


def test_validator_blocks_new_links_next_to_each_other():
    c = cfg()
    p = content.parse(DRAFT_RAW, SETTINGS_SKIP)
    n, shortlist = scoring.plan(scoring.score_targets(inventory(), _draft(p), c), _draft(p), c)
    ids = {f"T{i}": x for i, x in enumerate(shortlist, 1)}
    by_title = {v.target.title: k for k, v in ids.items()}
    a = next(s for s in p.sentences.values() if s.text.startswith("Every production AI system"))
    b = next(s for s in p.sentences.values() if s.text.startswith("Teams usually start"))
    edits = [
        {"sentence_id": a.id, "target_id": by_title["IIT Delhi - AI Forward Deployed Engineering"],
         "anchor": "forward deployed engineering programme",
         "rewritten": "Every production AI system needs a way to prove it works before customers rely on it, a core forward deployed engineering programme skill."},
        {"sentence_id": b.id, "target_id": by_title["RAG vs Fine-Tuning"], "anchor": "retrieval augmented generation",
         "rewritten": b.text},
    ]
    res = validate.check(edits, ids, p, 3, c.settings)
    assert len(res.accepted) == 1
    assert res.rejected[0][1] in {"too close to another link", "paragraph already has a new link",
                                  "program link in a sentence that isn't about learning or the programme's topic",
                                  "program links must use the writer's own words (no inserted phrase)"}


# ---------- program pages file ----------

from linker.config import CONFIG_DIR, load_programs
from linker.inventory import Target


def test_program_pages_csv_parsing(tmp_path):
    f = tmp_path / "p.csv"
    f.write_text(
        "id,active,name,url,categories,anchors\n"
        "a,yes,Prog A,https://www.interviewbit.com/varsity/iit-delhi/x?utm_source=ads&gclid=1,Cat 1|Cat 2,kw one|kw two\n"
        "b,no,Prog B,https://www.interviewbit.com/varsity/iit-delhi/y,Cat 3,kw\n", encoding="utf-8")
    progs = load_programs(f)
    assert [p.id for p in progs] == ["a"]                                 # inactive row skipped
    assert progs[0].url == "https://www.interviewbit.com/varsity/iit-delhi/x"   # tracking stripped, no slash added
    assert progs[0].categories == ["Cat 1", "Cat 2"] and progs[0].keywords == ["kw one", "kw two"]


def test_shipped_program_pages_are_valid():
    progs = load_programs(CONFIG_DIR / "program_pages.csv")
    assert len(progs) == 7
    assert all(p.url.startswith("https://www.interviewbit.com/varsity/") for p in progs)
    assert all(len(p.keywords) >= 3 for p in progs)


def test_inserted_link_uses_exact_program_url():
    c = cfg()
    p = content.parse(DRAFT_RAW, SETTINGS_SKIP)
    inv = [t for t in inventory() if t.type != "program"] + [
        Target("https://www.interviewbit.com/varsity/iit-delhi/ai-engineering/", "IIT Delhi - AI Forward Deployed Engineering",
               "program", ["Forward Deployed Engineering"], ["forward deployed engineering programme"],
               program_id="fde", href="https://www.interviewbit.com/varsity/iit-delhi/ai-engineering")]
    wp, ctl, llm = FakeWP(DRAFT_RAW), FakeControl(), FakeLLM()
    process_post(Row(), wp, inv, {7: "Forward Deployed Engineering"}, llm, c, ctl, dry_run=False)
    assert 'href="https://www.interviewbit.com/varsity/iit-delhi/ai-engineering"' in wp.updated


# ---------- preview tool ----------

RENDERED = """<p>Intro paragraph that the linker always leaves alone for safety.</p>
<h2>Why it matters</h2>
<p>Every production AI system needs a way to prove it works before customers rely on it. Teams usually start with retrieval augmented generation because it&#8217;s cheaper than training a model.</p>
<p>Prompt injection is one of the risks that forward deployed engineers have to defend against &#8211; on every customer project they take on.</p>
<p>Many engineers build these skills through a forward deployed engineering programme rather than on the job alone, because the feedback loop is faster.</p>
<script>alert(1)</script>"""


def test_locate_handles_wordpress_entities():
    p = content.parse(RENDERED, SETTINGS_SKIP)
    texts = {s.text: s for s in p.sentences.values()}
    s = next(v for k, v in texts.items() if k.startswith("Teams usually start"))
    assert "&#8217;" in s.html                          # matched the entity form in the HTML
    new = content.apply_edits(p, [{"sentence_id": s.id, "anchor": "retrieval augmented generation",
                                   "rewritten": s.text, "url": "https://x/rag/"}])
    assert '<a href="https://x/rag/">retrieval augmented generation</a>' in new


class FakePublicWP:
    authed = False

    def __init__(self, *a, **k):
        pass

    def published_post(self, ref):
        return {"id": 77, "status": "publish", "url": BASE + "llm-evals-for-fdes/", "title": "LLM Evals for FDEs",
                "category_ids": [7], "raw": RENDERED}

    def categories(self):
        return {7: "Forward Deployed Engineering"}

    def published_posts(self):
        return []


def test_preview_end_to_end(tmp_path, monkeypatch):
    from linker import preview, inventory as inv_mod
    monkeypatch.setattr(preview, "WordPress", FakePublicWP)
    monkeypatch.setattr(preview, "build_inventory", lambda *a, **k: inventory())
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    import linker.llm as llm_mod
    monkeypatch.setattr(llm_mod, "Gemini", lambda *a, **k: FakeLLM())
    for k in ("GOOGLE_SERVICE_ACCOUNT_JSON", "MASTERSHEET_ID", "WP_USERNAME", "WP_APP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)

    preview.main(["--post", BASE + "llm-evals-for-fdes/", "--keyword", "llm evaluation", "--out", str(tmp_path)])
    report = json.loads((tmp_path / "preview.json").read_text())
    page = (tmp_path / "preview.html").read_text()
    assert len(report["accepted"]) == 2
    assert 'class="ln-new"' in page and "<script>" not in page
    assert report["existing_internal"] == 0


def test_preview_plan_only_needs_no_key(tmp_path, monkeypatch):
    from linker import preview
    monkeypatch.setattr(preview, "WordPress", FakePublicWP)
    monkeypatch.setattr(preview, "build_inventory", lambda *a, **k: inventory())
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    preview.main(["--post", "llm-evals-for-fdes", "--plan-only", "--out", str(tmp_path)])
    report = json.loads((tmp_path / "preview.json").read_text())
    assert report["new_links_planned"] >= 1 and report["accepted"] == []


# ---------- WordPress fetching resilience ----------

class _Resp:
    def __init__(self, body, status=200, pages=1):
        self.text, self.status_code = body, status
        self.content = body.encode()
        self.headers = {"X-WP-TotalPages": str(pages), "content-type": "application/json"}

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def test_published_posts_survives_empty_responses(monkeypatch):
    from linker import wordpress
    monkeypatch.setattr(wordpress.time, "sleep", lambda s: None)
    meta = json.dumps([{"id": 1, "link": BASE + "a", "title": {"rendered": "A &#8211; B"}, "categories": [7]}])
    calls = {"meta": 0}

    def fake_get(url, params=None, timeout=None):
        if "content" in params["_fields"]:
            return _Resp("")                                  # article bodies always fail
        calls["meta"] += 1
        return _Resp("" if calls["meta"] == 1 else meta)      # first try empty, then fine

    wp = wordpress.WordPress(BASE, None, None, ["draft"])
    monkeypatch.setattr(wp.s, "get", fake_get)
    posts = wp.published_posts()
    assert [p.title for p in posts] == ["A – B"] and posts[0].content_html == ""
    assert calls["meta"] == 2


# ---------- keeping the LLM call small and resilient ----------

def test_select_sentences_caps_and_prefers_relevant():
    from linker.pipeline import select_sentences
    from linker.content import Sentence
    offered = {f"p{i}.s0": Sentence(f"p{i}.s0", i, f"Filler sentence number {i} about nothing in particular here.", "", i)
               for i in range(100)}
    offered["p50.s0"] = Sentence("p50.s0", 50, "Prompt injection is a real risk for every deployed system.", "", 50)
    c = cfg()
    p = content.parse(DRAFT_RAW, SETTINGS_SKIP)
    _, shortlist = scoring.plan(scoring.score_targets(inventory(), _draft(p), c), _draft(p), c)
    picked = select_sentences(offered, shortlist, 40)
    assert len(picked) == 40 and "p50.s0" in picked
    assert list(picked) == sorted(picked, key=lambda k: int(k[1:].split(".")[0]))   # article order kept


def test_gemini_retries_on_504(monkeypatch):
    from google.genai import errors
    import linker.llm as llm_mod
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    g = llm_mod.Gemini("k", "gemini-3.8-flash")
    calls = {"n": 0}

    class R:
        text = '{"edits": [{"sentence_id": "p1.s0"}]}'

    def fake(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise errors.ServerError(504, {"error": {"code": 504, "message": "Deadline expired", "status": "DEADLINE_EXCEEDED"}})
        assert kw["config"].thinking_config.thinking_level.value == "LOW"
        return R()

    monkeypatch.setattr(g.client.models, "generate_content", fake)
    assert g.propose("x") == [{"sentence_id": "p1.s0"}] and calls["n"] == 2


# ---------- anchor quality (cases from the first live preview) ----------

QPE_TITLE = "Quantum Phase Estimation Explained Like You're 15"


def _t(title, kws, url="https://www.interviewbit.com/varsity/blog/x/"):
    return Target(url, title, "blog", ["Quantum Computing"], kws)


@pytest.mark.parametrize("anchor,target,expect", [
    ("phase estimation quantum circuit", _t("Quantum Circuit", ["quantum circuit"]),
     "anchor uses this article's own topic for another page"),
    ("applications of quantum phase estimation", _t("Quantum Computing Applications", ["quantum computing applications"]),
     "anchor's main word doesn't describe the target page"),
    ("On real hardware", _t("Quantum Hardware: How Quantum Computers Actually Work", ["quantum hardware"]),
     "anchor starts with a filler word"),
    ("noisy intermediate-scale quantum", _t("NISQ Computing Explained", ["nisq computing", "noisy intermediate-scale quantum"]), None),
    ("Hadamard gates", _t("Quantum Gates Explained: From Hadamard to CNOT", ["quantum gates"]), None),
    ("quantum phase estimation", _t("Shor's Algorithm", ["shor's algorithm"]), "anchor doesn't describe the target page"),
])
def test_anchor_rules(anchor, target, expect):
    from linker.validate import anchor_problem
    assert anchor_problem(anchor, target, QPE_TITLE) == expect


def test_near_duplicate_targets_deduped():
    from linker.scoring import Candidate, dedupe_topics
    a = Candidate(_t("Quantum Gates Explained: From Hadamard to CNOT", ["quantum gates"], "u1"), 0.9, 1, "same", None)
    b = Candidate(_t("Hadamard Gate", ["hadamard gate"], "u2"), 0.8, 1, "same", None)
    c = Candidate(_t("NISQ Computing Explained", ["nisq"], "u3"), 0.7, 1, "same", None)
    assert [x.target.url for x in dedupe_topics([b, a, c])] == ["u1", "u3"]


def test_gemini_falls_back_when_overloaded(monkeypatch):
    from google.genai import errors
    import linker.llm as llm_mod
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    g = llm_mod.Gemini("k", "gemini-3.8-flash", fallback_models=["gemini-3.6-flash"])
    seen = []

    class R:
        text = '{"edits": []}'

    def fake(model, contents, config):
        seen.append(model)
        if model == "gemini-3.8-flash":
            raise errors.ServerError(503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}})
        return R()

    monkeypatch.setattr(g.client.models, "generate_content", fake)
    assert g.propose("x") == [] and g.used_model == "gemini-3.6-flash"
    assert seen == ["gemini-3.8-flash"] * 3 + ["gemini-3.6-flash"]


def test_common_words_and_repeated_term_rule():
    from linker.pipeline import common_words
    inv = [Target(f"u{i}", f"Quantum Topic {i}", "blog", [], []) for i in range(10)] + \
          [Target("h1", "Hadamard Gate", "blog", [], []), Target("h2", "Quantum Gates: From Hadamard to CNOT", "blog", [], [])]
    cw = common_words(inv)
    assert "quantum" in cw and "hadamard" not in cw


def test_repair_round_fills_rejected_slots():
    from linker import pipeline
    c = cfg()
    p = content.parse(DRAFT_RAW, SETTINGS_SKIP)

    class TwoPassLLM(FakeLLM):
        calls = 0

        def propose(self, prompt):
            TwoPassLLM.calls += 1
            good = super().propose(prompt)
            if TwoPassLLM.calls == 1:            # first pass: one good edit + one bad anchor
                bad = dict(good[1], anchor="On the job", rewritten=good[1]["rewritten"].replace(
                    "a structured forward deployed engineering programme", "structured training"))
                return [good[0], bad]
            assert "SECOND PASS" in prompt and "filler word" in prompt
            return [good[1]]                     # second pass: the fixed edit

    a = pipeline.analyse(DRAFT_RAW, "LLM Evals for FDEs", ["Forward Deployed Engineering"], "llm evaluation",
                         99, inventory(), c)
    res = pipeline.propose(a, TwoPassLLM(), c)
    assert TwoPassLLM.calls == 2 and len(res.checked.accepted) == 2
    assert any("filler" in r for _, r in res.checked.rejected)


# ---------- cases from the second live preview ----------

def test_title_phrases_make_good_anchor_ideas():
    from linker.inventory import _title_phrase as t
    assert t("The Hadamard Gate Explained Better Than Your Textbook") == "hadamard gate"
    assert t("Best Quantum Computing Certifications in India (2026): Which Ones Matter") == "quantum computing certifications"
    assert t("Qubit vs Bit: The Real Difference Between Quantum and Classical Information") == "qubit vs bit"


def _single(anchor, original, rewritten, accepted_anchor=None, target_title="Superposition in Quantum Computing",
            kws=("superposition in quantum computing",), common=("quantum", "computing")):
    from linker.content import ParsedDraft, Sentence
    s = Sentence("p5.s0", 5, original, original, 20)
    parsed = ParsedDraft("", {}, {"p5.s0": s}, 1000, [], set(), {})
    ids = {"T1": scoring.Candidate(Target("https://x/t1/", target_title, "blog", ["Q"], list(kws)), 1, 1, "same", None)}
    edits = []
    if accepted_anchor:
        s2 = Sentence("p9.s0", 9, f"Many teams study {accepted_anchor} early on in their journey.", "", 40)
        parsed.sentences["p9.s0"] = s2
        ids["T2"] = scoring.Candidate(Target("https://x/t2/", "Other", "blog", ["Q"], [accepted_anchor]), 1, 1, "same", None)
        edits.append({"sentence_id": "p9.s0", "target_id": "T2", "anchor": accepted_anchor, "rewritten": s2.text})
    edits.append({"sentence_id": "p5.s0", "target_id": "T1", "anchor": anchor, "rewritten": rewritten})
    return validate.check(edits, ids, parsed, 5, cfg().settings, None, "Quantum Phase Estimation", set(common))


def test_forced_topic_insertion_rejected():
    orig = "Quantum phase describes which way a state's complex amplitude points, not how big it is."
    res = _single("superposition in quantum computing", orig,
                  "Central to superposition in quantum computing, quantum phase describes which way a state's complex amplitude points, not how big it is.")
    assert res.rejected and res.rejected[0][1] == "blog links must use the writer's own words (anchor isn't in the sentence)"


def test_common_words_dont_make_anchors_similar():
    orig = "The practical value of quantum computing applications depends on the problem and the hardware."
    res = _single("quantum computing applications", orig, orig, accepted_anchor="superposition in quantum computing",
                  target_title="Quantum Computing Applications", kws=("quantum computing applications",))
    assert len(res.accepted) == 2, res.rejected


# ---------- cases from the third live preview ----------

def test_vague_noun_anchor_rejected():
    from linker.validate import anchor_problem
    t = Target("u", "Superposition in Quantum Computing", "blog", [], ["superposition"])
    assert anchor_problem("parts of their superposition", t, "Quantum Phase Estimation") == "anchor starts with a filler word"


def test_punctuation_only_rewrite_rejected():
    orig = "That is why full-scale QPE is hard on today's noisy intermediate-scale quantum (NISQ) hardware."
    res = _single("NISQ hardware", orig, orig.replace("(NISQ) hardware", "(NISQ hardware)"),
                  target_title="NISQ Explained", kws=("nisq",))
    assert res.rejected and res.rejected[0][1] in ("rewrite only changed punctuation", "blog links must use the writer's own words (anchor isn't in the sentence)",
                                                   "anchor's main word doesn't describe the target page")


def test_verbatim_anchor_keeps_writer_sentence():
    orig = "Shor's algorithm factors a number N by finding the period of a pattern."
    res = _single("Shor's algorithm", orig, "Famous Shor's algorithm factors a number N by finding the period of a pattern.",
                  target_title="Shor's Algorithm Explained", kws=("shor's algorithm",))
    assert res.accepted and res.accepted[0]["rewritten"] == orig



# ---------- cases from the Gen AI preview (instruction tuning post) ----------

def test_single_word_anchor_for_distinctive_topic():
    from linker.validate import _ok_single_word
    rlhf = Target("u", "RLHF Explained: The 3-Step Pipeline + RLHF vs DPO", "blog", [], ["rlhf"])
    quant = Target("u", "LLM Quantization Explained: GPTQ vs AWQ vs GGUF", "blog", [], ["llm quantization"])
    assert _ok_single_word("RLHF", rlhf, {"llm", "ai"})
    assert _ok_single_word("Quantization", quant, {"llm", "ai"})
    assert not _ok_single_word("LLM", quant, {"llm", "ai"})          # common word
    assert not _ok_single_word("DPO", quant, set())                   # not the target's topic


def test_padding_words_rejected_for_blog_links():
    orig = "LIMA's authors found validation perplexity did not track generation quality."
    res = _single("perplexity in NLP", orig,
                  "LIMA's authors found validation perplexity in NLP benchmarks did not track generation quality.",
                  target_title="Perplexity in NLP", kws=("perplexity in nlp",), common=())
    assert res.rejected and res.rejected[0][1] == "blog links must use the writer's own words (anchor isn't in the sentence)"


def test_case_insensitive_anchor_keeps_original():
    orig = "Alignment methods such as RLHF and DPO can then optimise its behaviour using preference data."
    res = _single("alignment methods such as RLHF", orig,
                  "Popular alignment methods such as RLHF and DPO can then optimise its behaviour using preference data.",
                  target_title="RLHF Explained", kws=("rlhf",), common=())
    assert res.accepted and res.accepted[0]["rewritten"] == orig
    assert res.accepted[0]["anchor"] == "Alignment methods such as RLHF"


def test_preview_accepts_edit_links_ids_and_urls():
    from linker.preview import _post_ref
    assert _post_ref("https://www.interviewbit.com/varsity/blog/wp-admin/post.php?post=1573") == "1573"
    assert _post_ref("https://www.interviewbit.com/varsity/blog/wp-admin/post.php?post=1573&action=edit") == "1573"
    assert _post_ref("https://www.interviewbit.com/varsity/blog/?p=1573") == "1573"
    assert _post_ref(" 1573 ") == "1573"
    assert _post_ref("https://www.interviewbit.com/varsity/blog/rlhf/") == "https://www.interviewbit.com/varsity/blog/rlhf/"


def test_hyphenated_anchor_counts_as_two_words():
    from linker.validate import _word_count, _ok_single_word
    assert _word_count("prompt-injection") == 2 and _word_count("RLHF") == 1
    orig = "Agents are exposed to prompt-injection whenever they read untrusted web pages."
    res = _single("prompt-injection", orig, orig, target_title="Prompt Injection Attacks",
                  kws=("prompt injection",), common=())
    assert res.accepted, res.rejected


def test_manual_post_id_not_in_sheet_still_runs(monkeypatch):
    from linker import main as m
    rows = []
    assert m.ready_rows(rows, cfg().settings, set(), 1573) == []


# ---------- audit of live posts (sheet only, never WordPress) ----------

def test_audit_logs_to_sheet_and_never_writes_wordpress(tmp_path, monkeypatch):
    from linker import audit
    from linker.wordpress import PublishedPost
    import linker.llm as llm_mod
    import linker.sheets as sheets_mod

    posts = [PublishedPost(77, BASE + "llm-evals-for-fdes/", "LLM Evals for FDEs", [7], RENDERED),
             PublishedPost(78, BASE + "takt-time/", "Takt Time", [9], "<p>Short.</p>")]
    created = {}

    class WP:
        def __init__(self, base, user, pw, statuses):
            created["user"], created["pw"] = user, pw
        def categories(self): return {7: "Forward Deployed Engineering", 9: "AI Powered Operations"}
        def published_posts(self): return posts
        def update_draft(self, *a): raise AssertionError("audit must never write to WordPress")

    class Sheet:
        def __init__(self, *a): self.summary, self.links = [], []; created["sheet"] = self
        def append(self, s, l): self.summary.append(s); self.links.extend(l)

    monkeypatch.setattr(audit, "WordPress", WP)
    monkeypatch.setattr(audit, "build_inventory", lambda *a, **k: inventory())
    monkeypatch.setattr(llm_mod, "gemini_from_config", lambda *a, **k: FakeLLM())
    monkeypatch.setattr(sheets_mod, "AuditSheet", Sheet)
    monkeypatch.setattr(audit.time, "sleep", lambda s: None)
    for k, v in {"GEMINI_API_KEY": "x", "GOOGLE_SERVICE_ACCOUNT_JSON": "{}", "CONTROL_SHEET_ID": "c"}.items():
        monkeypatch.setenv(k, v)
    for k in ("MASTERSHEET_ID", "WP_USERNAME", "WP_APP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)

    audit.main(["--posts", BASE + "llm-evals-for-fdes/", "--out", str(tmp_path)])
    sheet = created["sheet"]
    assert created["user"] is None and created["pw"] is None          # read-only client
    assert len(sheet.summary) == 1 and sheet.summary[0][3] == BASE + "llm-evals-for-fdes/"
    assert len(sheet.links) == 2 and all(len(r) == 12 for r in sheet.links)
    assert (tmp_path / "index.html").exists() and (tmp_path / "llm-evals-for-fdes.html").exists()


def test_audit_sample_spreads_across_categories():
    from linker.audit import pick_posts
    from linker.wordpress import PublishedPost
    posts = [PublishedPost(i, f"u{i}", f"T{i}", [1 if i < 10 else 2], "") for i in range(20)]
    chosen = pick_posts(posts, {1: "A", 2: "B"}, 4, None, seed=1, explicit=[])
    assert len(chosen) == 4 and {p.category_ids[0] for p in chosen} == {1, 2}
    assert [p.id for p in pick_posts(posts, {1: "A", 2: "B"}, 4, None, 1, [])] == [p.id for p in chosen]  # seed repeats
    only_b = pick_posts(posts, {1: "A", 2: "B"}, 3, "b", seed=2, explicit=[])
    assert all(p.category_ids == [2] for p in only_b)



# ---------- cases from the first live-post audit ----------

def test_blog_link_cannot_replace_or_flip_writer_words():
    for orig, bad, anchor in [
        ("Today, the clearest opportunities are campaign orchestration, paid-media optimisation, lifecycle journeys.",
         "Today, the clearest opportunities are campaign orchestration, performance marketing optimisation, lifecycle journeys.",
         "performance marketing"),
        ("Traditional marketing automation follows predefined rules, while an AI agent can decide what to do next.",
         "Traditional AI marketing automation follows predefined rules, while an AI agent can decide what to do next.",
         "AI marketing automation"),
    ]:
        res = _single(anchor, orig, bad, target_title="Some Page", kws=(anchor.lower(),), common=())
        assert not res.accepted and "writer's own words" in res.rejected[0][1]


def _program_single(orig, rewritten, anchor="generative AI course", insert_words=6):
    from linker.content import ParsedDraft, Sentence
    settings = cfg().settings
    settings["linking"]["program_max_added_words"] = insert_words
    s = Sentence("p5.s0", 5, orig, orig, 20)
    parsed = ParsedDraft("", {}, {"p5.s0": s}, 1000, [], set(), {})
    t = Target("https://www.interviewbit.com/varsity/iit-delhi/generative-ai", "IIT Delhi - Generative AI",
               "program", ["Generative AI"], ["generative AI programme", "generative AI course"], program_id="genai")
    ids = {"T1": scoring.Candidate(t, 1, 1, "same", None, required=True)}
    return validate.check([{"sentence_id": "p5.s0", "target_id": "T1", "anchor": anchor, "rewritten": rewritten}],
                          ids, parsed, 3, settings, None, "Cursor vs Claude Code", set())


def test_program_links_are_verbatim_by_default():
    # the shipped default: no inserted phrases, even in a learning sentence (batch-1 backfill cases)
    assert cfg().settings["linking"]["program_max_added_words"] == 0
    r = _program_single("Most engineers pick up these skills through structured training rather than on the job.",
                        "Most engineers pick up these skills through structured training, like a generative AI course, rather than on the job.",
                        insert_words=0)
    assert not r.accepted and "writer's own words" in r.rejected[0][1]
    # the writer's own words are fine
    orig = "Many engineers now take a generative AI course before switching roles."
    r = _program_single(orig, orig, insert_words=0)
    assert r.accepted and r.accepted[0]["rewritten"] == orig


def test_program_link_rules():
    # forced into an unrelated sentence
    r = _program_single("The skill being built here is not tool operation.",
                        "The skill being built here, much like in a generative AI course, is not tool operation.")
    assert "isn't about learning" in r.rejected[0][1]
    # changed the writer's words (dropped the quotes)
    r = _program_single("Every engineer added “prompt engineering” to their career profile in the last two years.",
                        "Every engineer added prompt engineering to their career profile after a generative AI course in the last two years.")
    assert "changed the writer's words" in r.rejected[0][1]
    # clean insertion into a learning sentence is fine
    r = _program_single("Most engineers pick up these skills through structured training rather than on the job.",
                        "Most engineers pick up these skills through structured training, like a generative AI course, rather than on the job.")
    assert r.accepted


def test_title_phrase_doesnt_collapse_to_ai():
    from linker.inventory import _title_phrase as t
    assert t("AI for SEO: Tools, Workflows & What Actually Works") == "ai for seo"
    assert t("AI in Marketing Course: Syllabus, Tools, Fees") == "ai in marketing course"


def test_existing_links_counted_once_and_images_ignored():
    raw = ("<p>Intro text that is long enough to be skipped by the linker here.</p>"
           "<p>See the <a href='https://www.interviewbit.com/varsity/iit-delhi/ai-product-management/'>programme</a> now.</p>"
           "<p><a href='https://www.interviewbit.com/varsity/iit-delhi/ai-product-management/'><img src='x.png'></a></p>"
           "<p>Again the <a href='https://www.interviewbit.com/varsity/iit-delhi/ai-product-management/'>AI PM programme</a>.</p>")
    p = content.parse(raw, SETTINGS_SKIP, internal_prefixes=["https://www.interviewbit.com/varsity/"])
    assert p.existing_internal == 1
    assert list(p.duplicate_internal.values()) == [3]


# ---------- concurrency protections ----------

from datetime import datetime, timedelta, timezone


def _now_gmt(minutes_ago=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def test_recently_edited_draft_waits_unless_exempt():
    c = cfg()
    wp, ctl = FakeWP(DRAFT_RAW, modified_gmt=_now_gmt(3)), FakeControl()
    res = process_post(Row(), wp, inventory(), {7: "Forward Deployed Engineering"}, FakeLLM(), c, ctl, False)
    assert res["status"] == "waiting" and wp.updated is None and ctl.posts == []   # no sheet spam
    res = process_post(Row(), wp, inventory(), {7: "Forward Deployed Engineering"}, FakeLLM(), c, ctl, False,
                       skip_cooloff=True)                                           # event/manual run
    assert res["status"] == "linked" and wp.updated


def test_save_passes_version_stamp_and_conflict_is_retried():
    from linker.wordpress import Conflict
    c = cfg()
    wp, ctl = FakeWP(DRAFT_RAW), FakeControl()
    process_post(Row(), wp, inventory(), {7: "Forward Deployed Engineering"}, FakeLLM(), c, ctl, False)
    assert wp.update_kwargs == {"expected_modified": "2026-01-01T00:00:00", "allow_published": False}

    class ConflictWP(FakeWP):
        def update_draft(self, pid, raw, **kw):
            raise Conflict("saved by someone else")
    wp, ctl = ConflictWP(DRAFT_RAW), FakeControl()
    res = process_post(Row(), wp, inventory(), {7: "Forward Deployed Engineering"}, FakeLLM(), c, ctl, False)
    assert res["status"] == "retry_later"
    assert ctl.changes == [] and ctl.posts[-1][3] == "retry_later"   # not "linked" -> picked up next run


class _SeqSession:
    """Fake requests session: GETs return queued post states, POST records the payload."""
    def __init__(self, states):
        self.states, self.posted, self.auth, self.headers = list(states), None, None, {}

    def get(self, url, params=None, timeout=None):
        st = self.states.pop(0)
        return _Resp(json.dumps({"id": 5, "status": st[0], "link": "u", "title": {"raw": "T"}, "categories": [],
                                 "content": {"raw": "<p>x</p>"}, "modified_gmt": st[1]}))

    def post(self, url, json=None, timeout=None):
        import json as _j
        self.posted = json
        return _Resp(_j.dumps({"modified_gmt": "2026-02-02T00:00:00"}))


def test_wordpress_refuses_stale_or_published_writes():
    from linker.wordpress import Conflict, NotEditable, WordPress
    wp = WordPress(BASE, "u", "p", ["draft", "pending"])
    wp.s = _SeqSession([("draft", "2026-01-01T00:05:00")])
    with pytest.raises(Conflict):
        wp.update_draft(5, "<p>new</p>", expected_modified="2026-01-01T00:00:00")
    wp.s = _SeqSession([("publish", "2026-01-01T00:00:00")])
    with pytest.raises(NotEditable):
        wp.update_draft(5, "<p>new</p>", expected_modified="2026-01-01T00:00:00")
    wp.s = _SeqSession([("publish", "2026-01-01T00:00:00")])
    assert wp.update_draft(5, "<p>new</p>", expected_modified="2026-01-01T00:00:00",
                           allow_published=True) == "2026-02-02T00:00:00"
    assert wp.s.posted == {"content": "<p>new</p>"}


def test_link_check_records_removed_links_once():
    from linker.main import check_live_links
    c = cfg()

    class Ctl:
        def __init__(self): self.rows = []
        def checked_post_ids(self): return {9}
        def live_links(self):
            return {1: {"title": "A", "urls": {BASE + "rlhf/", BASE + "nisq/"}, "linked_at": "2026-01-01 10:00 UTC"},
                    2: {"title": "B", "urls": {BASE + "x/"}, "linked_at": "2026-01-01 10:00 UTC"},     # still a draft
                    9: {"title": "C", "urls": {BASE + "y/"}, "linked_at": "2026-01-01 10:00 UTC"}}     # already checked
        def log_link_check(self, row): self.rows.append(row)

    class WP:
        def get_draft(self, pid):
            raw = f'<p><a href="{BASE}rlhf/">RLHF</a></p>' if pid == 1 else "<p>x</p>"
            return {"status": "publish" if pid == 1 else "draft", "title": "T", "url": "u", "raw": raw}

    ctl = Ctl()
    res = check_live_links(ctl, WP(), c, now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert [r["post_id"] for r in res] == [1]
    assert ctl.rows[0][5:] == [2, 1, BASE + "nisq/"]


# ---------- backfill + event-triggered runs (wiring, with fakes) ----------

def _secrets_env(monkeypatch):
    for k, v in {"GEMINI_API_KEY": "x", "GOOGLE_SERVICE_ACCOUNT_JSON": "{}", "CONTROL_SHEET_ID": "c",
                 "MASTERSHEET_ID": "m", "WP_USERNAME": "u", "WP_APP_PASSWORD": "p"}.items():
        monkeypatch.setenv(k, v)


class _Ctl(FakeControl):
    def __init__(self, done=()):
        super().__init__()
        self._done = set(done)
    def done_post_ids(self, statuses=("linked",)): return set(self._done)
    def checked_post_ids(self): return set()
    def live_links(self): return {}


def test_backfill_picks_next_unprocessed_and_respects_dry_run(monkeypatch):
    from linker import backfill
    from linker.wordpress import PublishedPost
    import linker.llm as llm_mod
    _secrets_env(monkeypatch)
    posts = [PublishedPost(i, BASE + f"p{i}/", f"Post {i}", [7], RENDERED) for i in (10, 11, 12, 13)]
    ctl = _Ctl(done={10})
    wp = FakeWP(DRAFT_RAW, status="publish")

    class WP:
        def __init__(self, *a): pass
        def categories(self): return {7: "Forward Deployed Engineering"}
        def published_posts(self): return posts
        def get_draft(self, pid): return wp.get_draft(pid)
        def update_draft(self, pid, raw, **kw): wp.update_draft(pid, raw, **kw)

    class MS:
        def __init__(self, *a): pass
        def rows(self): return []

    monkeypatch.setattr(backfill, "ControlSheet", lambda *a: ctl)
    monkeypatch.setattr(backfill, "WordPress", WP)
    monkeypatch.setattr(backfill, "build_inventory", lambda *a, **k: inventory())
    monkeypatch.setattr("linker.sheets.MasterSheet", MS)
    monkeypatch.setattr(llm_mod, "gemini_from_config", lambda *a, **k: FakeLLM())

    backfill.main(["--next", "2"])                       # dry run
    assert wp.updated is None
    assert [p[0] for p in ctl.posts] == [11, 12]         # skips 10 (already done), takes the next two
    assert all(k["mode"] == "backfill" for k in ctl.post_kwargs)

    ctl.posts.clear(); ctl.post_kwargs.clear()
    backfill.main(["--posts", BASE + "p11/", "--live"])  # apply the reviewed post
    assert wp.updated and wp.update_kwargs["allow_published"] is True
    assert ctl.posts[-1][0] == 11 and ctl.posts[-1][3] == "linked"


def test_backfill_pick_by_category():
    from linker.backfill import pick
    from linker.wordpress import PublishedPost
    posts = [PublishedPost(1, "u1", "A", [1], ""), PublishedPost(2, "u2", "B", [2], ""), PublishedPost(3, "u3", "C", [2], "")]
    assert [p.id for p in pick(posts, {1: "Quantum Computing", 2: "Generative AI"}, {2}, [], 5, "generative ai")] == [3]


def test_event_run_skips_cooloff_only_for_triggered_drafts(monkeypatch):
    from linker import main as m
    import linker.llm as llm_mod
    _secrets_env(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "false")
    fresh = _now_gmt(1)
    drafts = {1940: FakeWP(DRAFT_RAW, modified_gmt=fresh), 1941: FakeWP(DRAFT_RAW, modified_gmt=fresh)}
    ctl = _Ctl()

    class WP:
        def __init__(self, *a): pass
        def categories(self): return {7: "Forward Deployed Engineering"}
        def published_posts(self): return []
        def get_draft(self, pid): return drafts[pid].get_draft(pid)
        def update_draft(self, pid, raw, **kw): drafts[pid].update_draft(pid, raw, **kw)

    def row(pid):
        r = Row(); r = type("R", (), dict(vars(Row)))(); r.post_id = pid
        r.content_status, r.publishing_status, r.topic = "Ready to Publish", "Yet to Publish", f"p{pid}"
        return r

    class MS:
        def __init__(self, *a): pass
        def rows(self): return [row(1940), row(1941)]

    monkeypatch.setattr(m, "ControlSheet", lambda *a: ctl)
    monkeypatch.setattr(m, "MasterSheet", MS)
    monkeypatch.setattr(m, "WordPress", WP)
    monkeypatch.setattr(m, "build_inventory", lambda *a, **k: inventory())
    monkeypatch.setattr(m, "gemini_from_config", lambda *a, **k: FakeLLM())
    monkeypatch.setattr(m, "OUT", __import__("pathlib").Path(str(__import__("tempfile").mkdtemp())))
    m.main(["--event-post-id", "1940"])
    assert drafts[1940].updated and drafts[1941].updated is None   # 1941 edited a minute ago, not triggered


# ---------- patch 17: cases from the first backfill dry run (operations batch) ----------

SCM = BASE + "supply-chain-management/"
NEXTGEN = BASE + "next-generation-supply-chain/"
NGLOG = BASE + "next-gen-logistics/"


def _ops_targets():
    return [
        Target(SCM, "Supply Chain Management: The Bullwhip Effect, Risk, and the AI Fix", "blog",
               ["Supply Chain Management"], ["supply chain management"], post_id=177),
        Target(NEXTGEN, "The Next Generation of Supply Chains: AI, Automation, and Autonomous Decision-Making",
               "blog", ["Supply Chain Management"], ["supply chain management", "next generation of supply chains"],
               post_id=165),
        Target(NGLOG, "Next-Gen Logistics: How AI Is Rebuilding Freight, Fulfilment & Last-Mile Networks", "blog",
               ["Business Operations"], ["next-gen logistics"], post_id=173),
    ]


def test_shared_keyword_stays_with_the_page_that_owns_it():
    from linker.inventory import assign_keyword_owners
    ts = _ops_targets()
    assign_keyword_owners(ts)
    assert "supply chain management" in ts[0].keywords             # /supply-chain-management/ owns it
    assert "supply chain management" not in ts[1].keywords         # the broader post loses it
    # no clear owner: nothing is removed
    a = Target(BASE + "a/", "Alpha Topic", "blog", [], ["shared phrase"])
    b = Target(BASE + "b/", "Beta Topic", "blog", [], ["shared phrase"])
    assign_keyword_owners([a, b])
    assert a.keywords == b.keywords == ["shared phrase"]


@pytest.mark.parametrize("anchor,idx,ok", [
    ("logistics costs", 2, False),             # 165 -> next-gen logistics: side word only
    ("logistics executives", 2, False),        # 177 -> next-gen logistics
    ("Supply chain management", 1, False),     # 177 -> next-gen supply chain (after ownership)
    ("next-gen logistics", 2, True),
    ("Supply chain management", 0, True),
])
def test_anchor_main_word_must_be_the_targets_topic(anchor, idx, ok):
    from linker.inventory import assign_keyword_owners
    from linker.validate import anchor_problem
    ts = _ops_targets()
    assign_keyword_owners(ts)
    assert (anchor_problem(anchor, ts[idx], "Logistics vs Supply Chain Management") is None) == ok


def test_same_anchor_words_cannot_link_two_pages():
    # post 160 linked "supply chain management" to two different pages
    res = _single("supply chain management",
                  "The traditional split was roughly: logistics executes, supply chain management plans and coordinates.",
                  "The traditional split was roughly: logistics executes, supply chain management plans and coordinates.",
                  accepted_anchor="supply chain management", target_title="Supply Chain Management",
                  kws=("supply chain management",), common=("supply", "chain", "management"))
    assert len(res.accepted) == 1
    assert res.rejected[0][1] == "same anchor words already link to another page"


def test_article_never_links_its_own_title_topic_elsewhere():
    # backfilled posts have no primary keyword: the title topic is used for the cannibalisation guard
    c = cfg()
    d = scoring.Draft(post_id=177, title="Supply Chain Management: The Bullwhip Effect, Risk, and the AI Fix",
                      categories=["Supply Chain Management"], primary_keyword="",
                      sentences=["Supply chain management coordinates sourcing, production and delivery."],
                      word_count=900, existing_links=set())
    other = Target(BASE + "scm-basics/", "SCM Basics", "blog", ["Supply Chain Management"],
                   ["supply chain management"], post_id=5)
    assert not scoring.score_targets([other], d, c)


REVIEWED = [
    {"anchor": "Prompt injection", "target_url": BASE + "prompt-injection/",
     "original_sentence": "Prompt injection is one of the risks that forward deployed engineers have to defend against on every customer project they take on.",
     "rewritten_sentence": "Prompt injection is one of the risks that forward deployed engineers have to defend against on every customer project they take on."},
    {"anchor": "operations management course", "target_url": "https://www.example.com/programs/fde/",
     "original_sentence": "Many engineers build these skills through a forward deployed engineering programme rather than on the job alone, because the feedback loop is faster.",
     "rewritten_sentence": "Many engineers build these skills through a forward deployed engineering programme, like an operations management course, rather than on the job alone, because the feedback loop is faster."},
    {"anchor": "takt time", "target_url": BASE + "takt-time/",
     "original_sentence": "A sentence the editor has since deleted from the post.",
     "rewritten_sentence": "A sentence the editor has since deleted from the post."},
]


class NoLLM:
    used_model = None

    def propose(self, prompt):
        raise AssertionError("apply-reviewed must not call the LLM")


def test_apply_reviewed_uses_dry_run_links_exactly_and_rechecks_them():
    c = cfg()
    wp, ctl = FakeWP(DRAFT_RAW, status="publish"), FakeControl()
    res = process_post(Row(), wp, inventory(), {7: "Forward Deployed Engineering"}, NoLLM(), c, ctl, False,
                       mode="backfill", allow_published=True, reviewed=REVIEWED)
    assert [a["anchor"] for a in res["accepted"]] == ["Prompt injection"]
    reasons = {r for _, r in [(x["edit"], x["reason"]) for x in res["rejected"]]}
    assert "sentence changed since the dry run" in reasons
    assert any("writer's own words" in r for r in reasons)          # old inserted programme phrase dropped
    assert 'href="https://www.interviewbit.com/varsity/blog/prompt-injection/">Prompt injection</a>' in wp.updated
    assert "applied reviewed dry-run links: 1 of 3" in ctl.posts[-1][6]


def test_model_used_is_logged():
    c = cfg()
    llm = FakeLLM()
    llm.used_model = "gemini-3.6-flash"
    ctl = FakeControl()
    process_post(Row(), FakeWP(DRAFT_RAW), inventory(), {7: "Forward Deployed Engineering"}, llm, c, ctl, True)
    assert "model: gemini-3.6-flash" in ctl.posts[-1][6]


def test_backfill_apply_reviewed_end_to_end(monkeypatch):
    from linker import backfill
    from linker.wordpress import PublishedPost
    import linker.llm as llm_mod
    _secrets_env(monkeypatch)
    posts = [PublishedPost(i, BASE + f"p{i}/", f"Post {i}", [7], RENDERED) for i in (10, 11, 12)]
    wp = FakeWP(DRAFT_RAW, status="publish")

    class Ctl(_Ctl):
        def dry_run_post_ids(self): return {11}
        def reviewed_edits(self, pid): return REVIEWED[:1] if pid == 11 else None

    ctl = Ctl()

    class WP:
        def __init__(self, *a): pass
        def categories(self): return {7: "Forward Deployed Engineering"}
        def published_posts(self): return posts
        def get_draft(self, pid): return wp.get_draft(pid)
        def update_draft(self, pid, raw, **kw): wp.update_draft(pid, raw, **kw)

    class MS:
        def __init__(self, *a): pass
        def rows(self): return []

    monkeypatch.setattr(backfill, "ControlSheet", lambda *a: ctl)
    monkeypatch.setattr(backfill, "WordPress", WP)
    monkeypatch.setattr(backfill, "build_inventory", lambda *a, **k: inventory())
    monkeypatch.setattr("linker.sheets.MasterSheet", MS)
    monkeypatch.setattr(llm_mod, "gemini_from_config", lambda *a, **k: NoLLM())

    backfill.main(["--next", "5", "--apply-reviewed"])       # only posts that HAVE a dry run
    assert [p[0] for p in ctl.posts] == [11]
    assert wp.updated and wp.update_kwargs["allow_published"] is True
    assert ctl.posts[-1][3] == "linked" and ctl.posts[-1][5] is False

    ctl.posts.clear(); wp.updated = None
    backfill.main(["--posts", BASE + "p12/", "--apply-reviewed"])   # never dry-run: skipped, not edited
    assert wp.updated is None and not ctl.posts


def test_reviewed_edits_reads_latest_dry_run_rows():
    from linker.sheets import ControlSheet

    class WS:
        def __init__(self, recs): self.recs = recs
        def get_all_records(self): return self.recs

    cs = ControlSheet.__new__(ControlSheet)
    cs.processed = WS([
        {"post_id": 160, "dry_run": "TRUE", "mode": "backfill", "run_at": "2026-11-03 08:00 UTC"},
        {"post_id": 160, "dry_run": "TRUE", "mode": "backfill", "run_at": "2026-11-03 09:32 UTC"},
        {"post_id": 161, "dry_run": "FALSE", "mode": "backfill", "run_at": "2026-11-03 09:40 UTC"},
    ])
    cs.processed_records = lambda: cs.processed.get_all_records()
    cs.changes = WS([
        {"post_id": 160, "dry_run": "TRUE", "run_at": "2026-11-03 08:00 UTC", "anchor": "old run"},
        {"post_id": 160, "dry_run": "TRUE", "run_at": "2026-11-03 09:31 UTC", "anchor": "supply chain management"},
        {"post_id": 160, "dry_run": "FALSE", "run_at": "2026-11-03 09:32 UTC", "anchor": "live row"},
    ])
    assert [r["anchor"] for r in cs.reviewed_edits(160)] == ["supply chain management"]
    assert cs.reviewed_edits(161) is None and cs.dry_run_post_ids() == {160}


# ---------- programme bridge sentence (added sentence, writer's sentences untouched) ----------

BRIDGE_RAW = """<!-- wp:paragraph -->
<p>Forward deployed engineers sit between customers and product teams, which makes the role unusual.</p>
<!-- /wp:paragraph -->

<!-- wp:paragraph -->
<p>Every production AI system needs a way to prove it works before customers rely on it. Teams usually start with retrieval augmented generation because it is cheaper than training a model.</p>
<!-- /wp:paragraph -->

<!-- wp:paragraph -->
<p>Prompt injection is one of the risks that forward deployed engineers have to defend against on every customer project they take on.</p>
<!-- /wp:paragraph -->

<!-- wp:paragraph -->
<p>Many engineers build these skills through structured training rather than on the job alone, because the feedback loop is faster.</p>
<!-- /wp:paragraph -->

<!-- wp:paragraph -->
<p>In the end, the teams that ship reliable systems are the ones that treat evaluation as part of the job from day one.</p>
<!-- /wp:paragraph -->
"""

GOOD_BRIDGE = ("If you want to build them in a structured way, a forward deployed engineering programme covers "
               "evaluation, deployment and customer work end to end.")


class BridgeLLM:
    def __init__(self, sentence=GOOD_BRIDGE, anchor="forward deployed engineering programme", pick="BEST FIT"):
        self.sentence, self.anchor, self.pick, self.prompts = sentence, anchor, pick, []

    def propose(self, prompt):
        self.prompts.append(prompt)
        if "ADD ONE NEW SENTENCE" in prompt:
            paras = re.findall(r"^(B\d+) \| (BEST FIT|LAST RESORT) \| (.*)$", prompt, re.M)
            pid = next((p for p, fit, txt in paras if fit == self.pick and txt.startswith("Many engineers")),
                       next(p for p, fit, _ in paras if fit == self.pick))
            return [{"paragraph_id": pid, "anchor": self.anchor, "sentence": self.sentence}]
        ids = dict(re.findall(r"^(T\d+) \| \w+ \| ([^|]+) \|", prompt, re.M))
        tid = {v.strip(): k for k, v in ids.items()}
        sid = re.search(r"^(p\d+\.s\d+): Prompt injection is one", prompt, re.M).group(1)
        return [{"sentence_id": sid, "target_id": tid["Prompt Injection Attacks"], "anchor": "Prompt injection",
                 "rewritten": "Prompt injection is one of the risks that forward deployed engineers have to defend against on every customer project they take on."}]


def test_programme_link_added_as_new_sentence_in_best_fit_paragraph():
    c = cfg()
    wp, ctl, llm = FakeWP(BRIDGE_RAW), FakeControl(), BridgeLLM()
    res = process_post(Row(), wp, inventory(), {7: "Forward Deployed Engineering"}, llm, c, ctl, False)
    assert res["status"] == "linked" and len(res["accepted"]) == 2
    bridge_prompt = llm.prompts[-1]
    assert re.search(r"^B\d+ \| BEST FIT \| Many engineers build these skills", bridge_prompt, re.M)  # learning paragraph
    assert re.search(r"LAST RESORT \| In the end", bridge_prompt)                       # conclusion last
    assert not re.search(r"^B\d+ \| .* \| Prompt injection", bridge_prompt, re.M)      # already has a link
    # appended to the END of the training paragraph; the writer's sentence is unchanged
    assert ("because the feedback loop is faster. If you want to build them in a structured way, a "
            '<a href="https://www.example.com/programs/fde/">forward deployed engineering programme</a>') in wp.updated
    assert "programme link added as a new sentence" in ctl.posts[-1][6]
    assert "required program link not placed" not in ctl.posts[-1][6]
    row = [r for r in ctl.changes if r[3] == "https://www.example.com/programs/fde/"][0]
    assert row[7].startswith("[added after] Many engineers build") and row[8] == GOOD_BRIDGE


@pytest.mark.parametrize("sentence,anchor,reason", [
    ("A forward deployed engineering programme is the best way to land a top salary in this field.",
     "forward deployed engineering programme", "promotional claim"),
    ("Over 3 months, a forward deployed engineering programme covers evaluation, deployment and customer work.",
     "forward deployed engineering programme", "numbers"),
    ("If you want to go further, structured forward deployed engineering helps you practise this with real customers.",
     "forward deployed engineering", "programme's anchor phrases"),       # topic anchor isn't a programme phrase
    ("A forward deployed engineering programme helps.", "forward deployed engineering programme", "length"),
])
def test_bad_added_sentences_are_rejected(sentence, anchor, reason):
    c = cfg()
    wp, ctl = FakeWP(BRIDGE_RAW), FakeControl()
    res = process_post(Row(), wp, inventory(), {7: "Forward Deployed Engineering"},
                       BridgeLLM(sentence, anchor), c, ctl, False)
    assert len(res["accepted"]) == 1
    assert any(reason in r["reason"] for r in res["rejected"])
    assert "required program link not placed" in ctl.posts[-1][6]


def test_no_added_sentence_when_programme_already_on_writers_words():
    c = cfg()
    llm = FakeLLM()             # places the programme link verbatim in DRAFT_RAW
    calls = []
    orig = llm.propose
    llm.propose = lambda p: calls.append(p) or orig(p)
    process_post(Row(), FakeWP(DRAFT_RAW), inventory(), {7: "Forward Deployed Engineering"}, llm, c, FakeControl(), True)
    assert not any("ADD ONE NEW SENTENCE" in p for p in calls)


def test_bridge_can_be_switched_off():
    c = cfg()
    c.settings["linking"]["program_bridge_sentence"] = False
    llm = BridgeLLM()
    res = process_post(Row(), FakeWP(BRIDGE_RAW), inventory(), {7: "Forward Deployed Engineering"}, llm, c,
                       FakeControl(), True)
    assert len(res["accepted"]) == 1 and len(llm.prompts) == 1


def test_apply_reviewed_replays_added_sentence():
    c = cfg()
    reviewed = [{"anchor": "forward deployed engineering programme", "target_url": "https://www.example.com/programs/fde/",
                 "original_sentence": "[added after] Many engineers build these skills through structured training rather than on the job alone, because the feedback loop is faster.",
                 "rewritten_sentence": GOOD_BRIDGE}]
    wp, ctl = FakeWP(BRIDGE_RAW, status="publish"), FakeControl()
    res = process_post(Row(), wp, inventory(), {7: "Forward Deployed Engineering"}, NoLLM(), c, ctl, False,
                       mode="backfill", allow_published=True, reviewed=reviewed)
    assert len(res["accepted"]) == 1 and GOOD_BRIDGE.split(",")[0] in wp.updated
    # paragraph edited since the dry run: dropped, not re-placed somewhere else
    edited = BRIDGE_RAW.replace("the feedback loop is faster.", "feedback arrives sooner.")
    wp2 = FakeWP(edited, status="publish")
    res = process_post(Row(), wp2, inventory(), {7: "Forward Deployed Engineering"}, NoLLM(), c, FakeControl(), False,
                       mode="backfill", allow_published=True, reviewed=reviewed)
    assert not res["accepted"] and "changed" in res["rejected"][0]["reason"]


def test_program_csv_has_topic_and_programme_anchors():
    progs = {p.id: p for p in load_programs(CONFIG_DIR / "program_pages.csv")}
    assert "AI-powered operations" in progs["ops"].topic_keywords
    assert "AI-powered operations programme" in progs["ops"].keywords
    assert all(p.topic_keywords for p in progs.values())
