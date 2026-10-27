"""The gold-source drafting script, on invented question files shaped like a benchmark item
(atoms with answer_source, a free-text sources list, verification evidence). Nothing here comes
from a real question set: every key, repository, path, URL and site name is made up."""
import yaml

from archi.evaluation.draft_gold_sources import draft_item, extract, main

ITEM = {
    "id": "q-demo-07",
    "question": "Why did the invented rule cleanup stop?",
    "atoms": [
        {"id": "a1", "text": "the cleanup job errored", "required": True, "answer_source": "ticket"},
        {"id": "a2", "text": "the function that deletes rules", "required": True, "answer_source": "source_code"},
        {"id": "a3", "text": "how many rules are stuck now", "required": False, "answer_source": "live_rucio"},
        {"id": "a4", "text": "the procedure", "required": True, "answer_source": "doc or ticket"},
        {"id": "a5", "text": "the current state", "required": False, "answer_source": "ticket+live_rucio"},
        {"id": "a6", "text": "the deployed config", "required": False, "answer_source": "repo_config"},
    ],
    "sources": [
        "repos/fakerepo @1a2b3c4d src/fakerepo/core/rules.py (delete_rule 10-42)",
        "jira:DEMOOPS-12",
        "T2_ZZ_Nowhere",
        "a conversation in a meeting",
    ],
    "verification": {"evidence": "See https://docs.example.org/ops/rules.html and "
                                 "https://tracker.example.org/browse/DEMOOPS-13; also repos/otherrepo/cfg/app.yaml."},
}


def test_references_of_every_kind_are_extracted():
    refs, found = extract("s", "repos/r @abcdef12 a/b.py (f 1-2), jira:AB-1, CD-22, T1_ZZ_Lab_Disk, "
                               "https://docs.example.org/x.html https://github.com/o/r HIG-19-001")
    by = {r["id"]: r for r in refs}
    assert found
    assert by["git_repo:r/a/b.py"]["key"] == "r/a/b.py" and "abcdef12" in by["git_repo:r/a/b.py"]["confidence"]
    assert by["jira:AB-1"]["confidence"].startswith("high") and by["jira:CD-22"]["confidence"].startswith("medium")
    assert by["site:T1_ZZ_Lab_Disk"]["source"] == "cric"
    assert by["docsite:https://docs.example.org/x.html"]["source"] == "docsite"
    assert not any("github.com" in r["id"] for r in refs)  # a repository URL is not a docsite page
    assert "jira:HIG-19" not in by  # an analysis id is not a Jira key


def test_a_question_is_drafted_with_atoms_notes_and_unresolved_items():
    draft = draft_item(ITEM["id"], ITEM)
    assert set(draft["gold"]) == {"git_repo:fakerepo/src/fakerepo/core/rules.py", "jira:DEMOOPS-12",
                                  "site:T2_ZZ_Nowhere", "docsite:https://docs.example.org/ops/rules.html",
                                  "jira:DEMOOPS-13", "git_repo:otherrepo/cfg/app.yaml"}
    assert draft["gold_sources"]["jira:DEMOOPS-12"] == {"source": "jira", "key": "DEMOOPS-12"}
    assert draft["gold_sources"]["git_repo:fakerepo/src/fakerepo/core/rules.py"] == {
        "source": "git_repo", "key": "fakerepo/src/fakerepo/core/rules.py"}
    atoms = {a["id"]: a for a in draft["atoms"]}
    assert set(atoms["a1"]["gold"]) == {"jira:DEMOOPS-12", "jira:DEMOOPS-13"}
    assert set(atoms["a2"]["gold"]) == {"git_repo:fakerepo/src/fakerepo/core/rules.py", "git_repo:otherrepo/cfg/app.yaml"}
    assert atoms["a3"]["gold"] == [] and "live system" in atoms["a3"]["note"]
    assert "docsite:https://docs.example.org/ops/rules.html" in atoms["a4"]["gold"]   # doc or ticket
    assert set(atoms["a5"]["gold"]) == {"jira:DEMOOPS-12", "jira:DEMOOPS-13"}         # counts through its ticket
    assert "delete_rule 10-42" in draft["notes"]["git_repo:fakerepo/src/fakerepo/core/rules.py"]
    assert draft["unresolved"] == ["sources[3]: no reference recognised"]


def test_an_atom_with_no_matching_reference_is_unresolved():
    draft = draft_item("q", {"atoms": [{"id": "a1", "answer_source": "source_code"},
                                       {"id": "a2", "answer_source": "carrier_pigeon"}],
                             "sources": ["jira:DEMOOPS-1"]})
    assert any("atom a1 (source_code): no git reference" in u for u in draft["unresolved"])
    assert any("atom a2: answer_source kind(s) ['carrier_pigeon']" in u for u in draft["unresolved"])


def test_the_command_writes_a_private_draft_beside_the_set(tmp_path, capsys):
    golden = tmp_path / "golden"
    golden.mkdir()
    (golden / "q-demo-07.yaml").write_text(yaml.safe_dump(ITEM))
    (golden / "q-demo-08.yaml").write_text(yaml.safe_dump({"atoms": [{"id": "a1", "answer_source": "monitoring"}]}))
    out = tmp_path / "private" / "gold_sources.draft.yaml"
    assert main(["--questions", str(golden), "--out", str(out)]) == 0
    text = out.read_text()
    assert text.startswith("# DRAFT gold_sources") and "Never commit it" in text
    data = yaml.safe_load(text)
    assert [q["id"] for q in data["questions"]] == ["q-demo-07", "q-demo-08"]
    assert "Why did the invented rule cleanup stop" not in text  # question text is not copied
    assert "unresolved items for review" in capsys.readouterr().out


def test_the_command_refuses_to_write_inside_the_repository(tmp_path, capsys):
    from pathlib import Path

    golden = tmp_path / "golden"
    golden.mkdir()
    (golden / "q.yaml").write_text(yaml.safe_dump(ITEM))
    inside = Path(__file__).resolve().parents[1] / "gold_sources.draft.yaml"
    assert main(["--questions", str(golden), "--out", str(inside)]) == 2
    assert "never committed" in capsys.readouterr().err and not inside.exists()
