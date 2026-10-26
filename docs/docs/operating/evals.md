# archi eval (coming)

`archi eval` is not in this branch yet. This page says what it is for, so
you can plan around it.

Each bundle will ship an eval set: operator questions, each with the
evidence that should answer it. `archi eval` will run the set against your
instance and report two scores:

- **Retrieval:** was the right evidence found?
- **Answer quality:** was the answer right, given the evidence?

`archi eval --context` will score retrieval alone and compare it with a
baseline that reads raw documents. That score feeds the
[context gate](../context-layer.md#the-context-gate): an automation can
require a minimum score before it runs live.

## Today

- There is no `archi eval` command. A gate that asks for a context-eval score
  fails, so the automation stays in shadow.
- The `instance-health` automation reads
  `eval/reports/*.json` in the instance if it exists. That file format is
  provisional until `archi eval` exists.
- To check an instance now, use `archi doctor` and ask the
  [quickstart](../quickstart.md)'s cited-answer question.
