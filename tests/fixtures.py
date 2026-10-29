"""Shared offline fixtures: a realistic draft, a small inventory, fake services."""
from linker.config import load_config
from linker.inventory import Target

DRAFT_RAW = """<!-- wp:paragraph -->
<p>Forward deployed engineers sit between customers and product teams, which makes the role unusual.</p>
<!-- /wp:paragraph -->

<!-- wp:heading -->
<h2 class="wp-block-heading">Why evaluation matters</h2>
<!-- /wp:heading -->

<!-- wp:paragraph -->
<p>Every production AI system needs a way to prove it works before customers rely on it. Teams usually start with retrieval augmented generation because it is cheaper than training a model.</p>
<!-- /wp:paragraph -->

<!-- wp:paragraph -->
<p>When a deployment goes wrong, <strong>observability</strong> is what saves the week. Tracing every call to the model lets an engineer see exactly where an answer went off the rails.</p>
<!-- /wp:paragraph -->

<!-- wp:paragraph -->
<p>Prompt injection is one of the risks that forward deployed engineers have to defend against on every customer project they take on.</p>
<!-- /wp:paragraph -->

<!-- wp:paragraph -->
<p>Many engineers build these skills through structured training rather than on the job alone, because the feedback loop is faster.</p>
<!-- /wp:paragraph -->

<!-- wp:heading -->
<h2 class="wp-block-heading">FAQs</h2>
<!-- /wp:heading -->

<!-- wp:paragraph -->
<p>Is prompt injection a real risk for forward deployed engineers in production systems today?</p>
<!-- /wp:paragraph -->
"""

BASE = "https://www.interviewbit.com/varsity/blog/"


def cfg():
    c = load_config(require_secrets=False)
    for p in c.programs:  # give programs test URLs so they enter the inventory
        p.url = f"https://www.example.com/programs/{p.id}/"
    return c


def inventory():
    return [
        Target(BASE + "rag-vs-fine-tuning/", "RAG vs Fine-Tuning", "blog",
               ["Forward Deployed Engineering"], ["rag vs fine tuning", "retrieval augmented generation"],
               inbound=1, post_id=1),
        Target(BASE + "prompt-injection/", "Prompt Injection Attacks", "blog",
               ["Forward Deployed Engineering"], ["prompt injection"], inbound=0, post_id=2),
        Target(BASE + "llm-observability/", "LLM Observability for FDEs", "blog",
               ["Forward Deployed Engineering"], ["llm observability"], inbound=3, post_id=3),
        Target(BASE + "rlhf/", "RLHF Explained", "blog",
               ["Generative AI"], ["rlhf"], inbound=0, post_id=4),
        Target(BASE + "takt-time/", "Takt Time", "blog",
               ["AI Powered Operations"], ["takt time"], inbound=0, post_id=5),
        Target("https://www.example.com/programs/fde/", "IIT Delhi - AI Forward Deployed Engineering",
               "program", ["Forward Deployed Engineering"],
               ["forward deployed engineering programme", "FDE course"], inbound=2, program_id="fde"),
        Target("https://www.example.com/programs/ops/", "IIM Trichy - AI-Powered Operations",
               "program", ["AI Powered Operations"], ["operations management course"], inbound=0,
               program_id="ops"),
    ]


class FakeControl:
    def __init__(self):
        self.posts, self.changes = [], []

    def log_post(self, *a, **k):
        self.posts.append(a)

    def log_changes(self, rows):
        self.changes.extend(rows)


class FakeWP:
    def __init__(self, raw, status="draft"):
        self.raw, self.status, self.updated = raw, status, None

    def get_draft(self, pid):
        return {"id": pid, "status": self.status, "title": "LLM Evals for FDEs",
                "category_ids": [7], "raw": self.raw}

    def update_draft(self, pid, raw):
        self.updated = raw
