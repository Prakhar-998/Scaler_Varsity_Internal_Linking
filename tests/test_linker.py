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
    assert reasons == ["unknown sentence_id", "unknown target_id", "generic anchor",
                       "rewrite introduced new numbers", "rewrite changed too much"]
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
             "rewritten": "Many engineers build these skills through a structured forward deployed engineering programme rather than on the job alone, because the feedback loop is faster."},
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
<p>Many engineers build these skills through structured training rather than on the job alone, because the feedback loop is faster.</p>
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
    assert res.rejected[0][1] in {"too close to another link", "paragraph already has a new link"}


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
