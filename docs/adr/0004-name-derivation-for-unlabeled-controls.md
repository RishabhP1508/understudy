# 0004. Derive names for unlabeled controls from table structure

Status: accepted (Phase 3)

## Context

The target app has no `<label for=>` anywhere, and its form fields are named `f1`, `f2`, and `f7`.
The browser therefore computes an empty accessible name for almost every control that matters. In
Phase 2 this showed up concretely: the node holding the savings balance came back as
`role=generic, name=""`, and the only way to address it was `ordinal=2`, meaning "the third anonymous
generic node on the page". That is a positional bet that breaks the moment a node appears above it,
and 3 of the 7 recorded steps depended on one.

Every caption in this app exists, but it lives in a neighbouring table cell rather than in a label.
The information is present in the accessibility tree; it is just not attached to the control.

## Decision

Derive a name for unlabeled elements by walking the structure of the single
`page.aria_snapshot(mode="ai")` tree, and record which strategy produced it as `name_source` on the
element. The ladder, in order:

1. `a11y` — the accessible name the browser computed. Used whenever it is non-empty.
2. `row_label` — climb to the ancestor `cell` whose parent is a `row`, then take the nearest
   preceding sibling `cell` in that row that has a name.
3. `column_header` — the cell at the same index in the preceding row.
4. `attr_name` — the element's `placeholder`, then its `name` attribute, read through the element's
   ref. Last resort, interactive roles only, capped, because it costs a round trip per element.

`name_source` is stored on every element, so a reader can always tell whether a name was authored by
the application or inferred by us. An inferred name is weaker evidence than a real one, and the
artifact should not hide the difference.

### Why the row rule is structural, not a backwards scan

The obvious implementation, "scan backwards for the nearest preceding cell that has a name", is
wrong, and measurably so. On the subaccount form it derives `Savings` for the account-type dropdown
instead of `Account Type`, because the `<td>` that contains the `<select>` gets its own accessible
name computed from the selected option's text, and that cell sits between the caption and the
control in document order. The rule has to climb to the control's own containing cell first and then
look at that cell's preceding siblings within the same row.

Measured, on the live fixture, naive versus structural:

    control                       naive              structural
    combobox (account type)       'Savings'          'Account Type'
    textbox  (nickname)           'Nickname'         'Nickname'
    textbox  (initial deposit)    'Initial Deposit'  'Initial Deposit'
    textbox  (member search)      'Member ID'        'Member ID'

### The frame boundary is not a barrier

The balance node sits 16 levels deep, inside an iframe, inside a cell, in a row whose first cell
reads `Savings Balance`. Because one `aria_snapshot` call already returns the whole cross-frame tree
as a single structure, climbing from the node to its containing cell crosses the iframe boundary
with no special handling. Measured: the structural rule derives `Savings Balance` for the node that
Phase 2 could only reach as `ordinal=2`.

## Tradeoff

A derived name is an inference about layout, not a contract the application offers. If the vendor
moves a caption from the left cell to a header row, `row_label` stops matching and `column_header`
has to catch it. That is why the strategy is recorded per element rather than silently applied, and
why Phase 4 keeps the ordinal as a lower-ranked fallback rather than deleting it. Derived names are
also only as good as the table being a real table; a layout built from nested divs would fall
through to `attr_name`.

## Alternatives considered

- **Keep using ordinals.** Rejected. It encodes position, which is the least stable property a page
  has, and it produces an artifact no human reviewer can check for correctness.
- **Ask the model to name the fields.** Rejected outright. It would put a model in the recording path
  for something a deterministic rule handles, and the name would then vary run to run.
- **Read the DOM and parse the table.** Rejected as the primary mechanism. It reintroduces the HTML
  dependency the whole design avoids, and the accessibility tree already carries the structure. It
  survives only as the capped last resort for `placeholder` and `name`.

### A note on `<label for=>`

The phase scope listed an explicit `<label for=>` strategy between the accessible name and the row
rule. It is not implemented as a separate step, because it cannot fire: when a label is correctly
associated, the browser folds it into the computed accessible name, so strategy 1 has already
returned it. A separate lookup would be unreachable code in an accessibility-first pipeline. Written
down here rather than implemented as a branch that never executes.

## Known issue, owed by Phase 5

Phase 2's redactor blanks a string because it contains a credential keyword. Observed live: the
model's own rationale "Enter the password to log in" was written to both `run.jsonl` and the
artifact as `[REDACTED]`, because the word "password" appears in ordinary prose.

That mechanism is wrong in both directions. It destroys a legitimate "why" for no benefit, and it
would happily pass a rationale that quoted an actual secret without using any keyword. Phase 5 must
drive redaction from FIELD SENSITIVITY instead: a value is redacted because of the field it was
typed into, and free prose is scanned only for the actual secret values in play, never for keywords.
`name_source` and the derived names from this ADR are what make that possible, because a field can
now be identified as the password field by its derived name rather than by guessing from content.
