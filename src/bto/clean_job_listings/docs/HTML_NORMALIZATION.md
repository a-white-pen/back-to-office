# HTML Normalization

**Status: LOCKED.** This document is the normative contract for converting
source job-description HTML into `description_text`. The broader observation,
quarantine and matching contracts are in [CONTRACT.md](CONTRACT.md).

## 1. Scope and output

One shared normalizer applies to HTML descriptions from:

- MyCareersFuture;
- SEEK AU and NZ;
- JobStreet SG;
- JobsDB HK and TH;
- Indeed in every collected market.

There is no source-specific HTML preprocessing or chrome removal. The narrow
raw-token protection in §6 applies to every input.

LinkedIn is outside this specification because it supplies plaintext. Its
source rule is defined in CONTRACT.md.

The output is deterministic Unicode text with meaningful structural boundaries
represented by LF. If no meaningful visible text remains, the result is NULL.

The priorities are to preserve meaningful JD text, preserve word and section
boundaries, avoid manufacturing presentation tokens, and produce byte-stable
output suitable for matching.

## 2. Processing order

For a non-NULL input, perform these steps in order:

1. Convert the input to a string.
2. Protect exact raw tokens matching `<R[0-9]+>` as defined in §6.
3. Feed the complete protected string once to one standard-library
   `html.parser.HTMLParser(convert_charrefs=True)` instance, then call
   `close()`.
4. Assemble visible parser data and logical-boundary events according to
   §§3–5.
5. For each logical line, restore protected raw tokens, apply token-local
   invisible-character repair, then general Unicode/control normalization.
6. Normalize the assembled line to Unicode NFC.
7. Collapse ordinary ASCII-space runs, trim the line, apply list numbering and
   indentation, discard empty lines, and join the remaining lines with one LF.

Character references are decoded exactly once by `HTMLParser`. Do not call
`html.unescape()` before parsing, decode parser output again, or reparse decoded
text. Thus `&amp;#x41;` becomes literal `&#x41;`, not `A`, and
`&lt;person&gt;` becomes visible `<person>` without being treated as markup.

## 3. HTML extraction rules

Preserve visible parser data in source order. HTML markup contributes only the
structural behavior defined here.

| Construct | Required behavior |
| :--- | :--- |
| Block-like start or end tag | Request a logical boundary. |
| `br` start tag | Request a logical boundary; emit no marker. |
| `hr` start tag | Request a logical boundary; emit no marker. |
| Inline tag | Transparent: retain contained visible text with no marker or boundary. |
| Ordinary unknown tag | Transparent: retain contained visible text with no marker or boundary. Do not preserve raw tag text. |
| `head`, `script`, `style`, `template` | Discard the tag and all contained content. Skipped content creates no separator. |
| HTML comment, declaration, processing instruction or unknown declaration | Ignore. |
| Anchor | Treat as inline text: retain visible children only. Discard `href` and every other attribute. |
| Attribute not given semantics by this specification | Ignore. |

The complete block-like set is:

```text
address article aside blockquote body details dialog div dl dt dd fieldset
figcaption figure footer form h1 h2 h3 h4 h5 h6 header hgroup html main nav
p section summary
```

Unsupported constructs and list attributes are exceptions to the ordinary
unknown-tag/ignored-attribute rules and are defined in §§5 and 8.

## 4. Text joining and logical boundaries

A boundary finalizes the current logical line when one exists. Repeated adjacent
boundaries coalesce; boundaries and whitespace-only parser data do not create
empty lines.

Adjacent parser text events concatenate exactly unless a defined structural
boundary separates them. **Never insert a space merely because two parser data
events are adjacent.** Inline formatting can occur inside a word:

```html
Software Enginee<strong>r</strong>
```

becomes:

```text
Software Engineer
```

while:

```html
<strong>Senior</strong><em>Engineer</em>
```

becomes `SeniorEngineer` because the source contains neither whitespace nor a
structural boundary between the text events.

After finalization, every nonempty logical line is separated by exactly one LF.
There is no leading or trailing LF and no blank-line run.

For example, `A<div>B</div>C` becomes:

```text
A
B
C
```

## 5. Lists

`ul`, `ol` and `li` use explicit stack state.

| Event or state | Required behavior |
| :--- | :--- |
| Open `ul` or `ol` | Request a boundary and push an unordered or ordered list context. |
| Close matching `ul` or `ol` | Finalize its open item, request a boundary and pop the context. |
| Mismatched list close | Ignore it; do not guess browser DOM repair. |
| Open `li` | If the current list already has an open item, finalize it; request a boundary and open the new item. |
| Close `li` | Finalize the current item and request a boundary. |
| Orphan `li` | Create an unordered depth-one context and process the item there. |
| EOF with open state | Finalize all visible current content deterministically. |
| Unordered item | Emit no generated bullet or dash. Preserve any employer-authored marker in its text. |
| Ordered item | Prefix its first nonempty line with its list-local decimal number, a period and one ASCII space. |
| Empty ordered item | Emit no line and consume no number. |
| Nested list | Start its own ordered counter when applicable; indent its item’s first line by two ASCII spaces for each depth below one. |
| Continuation in one item | Indent one additional two-space level beyond that item’s first-line indentation. |
| Item containing only child lists | Emit no line and consume no parent number. |

Each ordered list starts at 1. Decimal width is unrestricted. These attributes
are unsupported and produce the typed results shown in §8:

```text
ol[start]  ol[reversed]  ol[type]  li[value]
```

Example:

```html
<ol><li>A<ul><li>B</li></ul>tail</li><li></li><li>C</li></ol>
```

Output:

```text
1. A
  B
  tail
2. C
```

List indentation is stored in `description_text` but later collapses during
matching.

## 6. Raw-token protection

Before parsing, protect only literal tokens matching:

```regex
<R[0-9]+>
```

The token consists of literal angle brackets, uppercase ASCII `R`, and one or
more ASCII digits. Without protection, `HTMLParser` treats it as an unknown
start tag and loses the visible identifier. Restore every protected token
verbatim before Unicode normalization.

Placeholders must be deterministic and collision-safe: choose two private-use
code points absent from both the raw input and the characters obtained by
decoding its character references. Literal or entity-encoded private-use text
must never be mistaken for a placeholder. If no pair can be chosen, return the
typed unsupported reason `raw-token protection`.

Do not generalize this rule to other angle-bracket text. Entity-escaped angle
brackets are handled by exactly-once entity decoding.

## 7. Unicode, NFC and whitespace

### Token-local repair

A repair candidate is a maximal run containing only:

```text
A-Z a-z 0-9 @ . _ : / ? & = % + ~ # -
```

plus any of:

```text
U+200B U+200C U+200D U+2060 U+2061 U+2063 U+206F U+FEFF
```

Remove those invisible characters from the run only when the stripped ASCII
text either:

- contains `@` with at least one character before and after its first `@`; or
- starts exactly with lowercase `http://`, `https://` or `www.`.

Otherwise leave the run for general handling. Do not extend this repair to
bare domains or ordinary natural-language tokens.

### General handling

After token-local repair, apply this table character by character:

| Character | Action |
| :--- | :--- |
| Any character for which Python `str.isspace()` is true | Convert to one ASCII space before space-run collapse. |
| U+200B ZERO WIDTH SPACE | Convert to ASCII space. |
| U+00AD SOFT HYPHEN | Remove. |
| U+2060 WORD JOINER | Remove. |
| U+2061 FUNCTION APPLICATION | Remove. |
| U+2063 INVISIBLE SEPARATOR | Remove. |
| U+206F NOMINAL DIGIT SHAPES | Remove. |
| U+FEFF BOM / ZERO WIDTH NO-BREAK SPACE | Remove. |
| U+200C ZERO WIDTH NON-JOINER | Preserve outside qualifying token-local repair. |
| U+200D ZERO WIDTH JOINER | Preserve outside qualifying token-local repair. |
| Directionality marks and all other code points | Preserve. |

Normalize each assembled logical line to NFC after adjacent parser text chunks
have been joined, so combining sequences split across inline markup can
compose. Do not use NFKC or NFKD.

Then collapse runs of two or more ordinary ASCII spaces to one and trim the
line. List indentation is added after that collapse. Empty lines disappear;
the rest join with one LF. If none remain, return NULL.

## 8. Unsupported constructs and quarantine

### Normalizer-level result

A start tag for any of these constructs returns a typed unsupported reason:

```text
table thead tbody tfoot tr th td img pre
```

The same applies to `ol[start]`, `ol[reversed]`, `ol[type]` and
`li[value]`. Exhaustion of collision-safe raw-token placeholders returns
`raw-token protection`.

An unsupported construct inside already ignored
`head`/`script`/`style`/`template` content is ignored with that content. A
stray unsupported end tag does not trigger quarantine.

Do not silently invent extraction semantics, fall back to regular-expression
tag stripping, or drop the observation.

### Standardized-row result

For an affected payload-bearing observation:

- preserve the row, identity and every other independently derivable payload
  field;
- set `description_text`, `fingerprint` and `minhash_signature` to NULL;
- surface the typed reason in build reporting without storing it in Silver or
  logging JD prose/contact values.

### Build-level result

An isolated quarantine continues and makes the board × market result partial.
If every payload-bearing row for a board × market in the selected batch
quarantines, orchestration fails the build before merging that batch. This
systematic-failure check is not parser behavior.

## 9. Relationship to matching

`description_text` preserves every script and every rule in this document.
Matching derives a restricted view of it, and [CONTRACT.md](CONTRACT.md)
defines that view and everything downstream of it. Do not restate either here.

Only the boundary matters for this specification: the output bytes are a
matching input, so a change to them can change matching. Matching collapse
removes LF versus ordinary whitespace, logical line boundaries and list
indentation, so those are stored-text-only distinctions. Ordered-list
numbering, employer-authored bullets, protected angle-bracket tokens, Unicode
punctuation and preserved ZWNJ, ZWJ and directionality marks all survive
collapse and are therefore matching-visible. §13 turns that distinction into
the change policy.

## 10. Accepted information loss

| Discarded or flattened information | Reason |
| :--- | :--- |
| Bold, italic, underline and ordinary inline tag identity | Presentation only; visible text survives. |
| Heading level and wrapper/tag choice | Text and logical boundary survive; presentation hierarchy does not. |
| Arbitrary blank-line count | Layout only; logical separation survives. |
| Generated unordered-list marker | Browser presentation, not source text. |
| Difference between unordered items and equivalent consecutive text lines | Avoids injecting matching tokens; original `description_html` remains available. |
| Link destination and attributes | Commonly navigation, tracking or application metadata rather than visible JD prose. |
| CSS classes and most attributes | Presentation metadata. |
| Comments, declarations and ignored-content bodies | Non-visible document machinery. |
| Soft hyphen, BOM and selected invisible controls | Formatting/corruption artifacts that damage token identity. |
| Exact HTML nesting/wrapper depth | Meaningful list depth and logical boundaries are represented separately. |

An empty anchor therefore contributes no text. U+200B outside token-local
repair may split a visually intended word; that is the accepted consequence of
treating it as a break opportunity. ZWNJ and ZWJ are preserved outside
qualifying contact/URL repair because they may carry genuine linguistic or
grapheme meaning.

## 11. Representative golden examples

### Structural boundaries

```html
<p>Company:<br>Example Ltd</p><div>Remote</div>
```

```text
Company:
Example Ltd
Remote
```

### Unordered and nested ordered lists

```html
<ul><li>Build APIs</li><li><ol><li>Review</li><li>Ship</li></ol></li></ul>
```

```text
Build APIs
  1. Review
  2. Ship
```

The second unordered item has no visible line of its own and no marker.

### Inline joining

```html
Software Enginee<strong>r</strong> / <strong>Senior</strong><em>Engineer</em>
```

```text
Software Engineer / SeniorEngineer
```

### Exactly-once entities

```html
A&nbsp;&amp;&nbsp;B; &amp;#x41; and &lt;person&gt;
```

```text
A & B; &#x41; and <person>
```

### Raw registration token

```html
<p>ID <R123456></p>
```

```text
ID <R123456>
```

### Unicode repair and NFC

Input code points:

```text
jobs@exam<U+200B>ple.com Cafe<U+0301> A<U+2060>B
```

Output:

```text
jobs@example.com Café AB
```

### Preserved linguistic controls

Input and output code points are identical:

```text
می<U+200C>روم 👩<U+200D>💻 <U+200F>ABC<U+200E>
```

### Ignored content

```html
A<script>hidden()</script><style>p{}</style><!-- hidden -->B
```

```text
AB
```

### Unsupported row

```html
<p>Before</p><table><tr><td>Value</td></tr></table>
```

The normalizer returns typed reason `table`. The standardized row survives
with NULL `description_text`, `fingerprint` and `minhash_signature`; the
orchestrator then applies the batch-level rule in §8.

## 12. Test coverage requirements

Because executable tests are intentionally not committed, the implementation’s
golden/regression suite must retain byte-exact coverage by behavioral category:

- structural start/end boundaries, `br`/`hr`, repeated boundaries and final
  whitespace;
- inline joining, punctuation, ordinary unknown tags and malformed-but-parseable
  fragments;
- unordered, ordered, nested, mixed, empty, continuation, orphan, mismatched
  close and EOF list behavior;
- exactly-once named/numeric/double-encoded entities;
- raw-token protection, placeholder collision avoidance and placeholder
  exhaustion;
- every token-local and general Unicode/control action, NFC including across
  inline markup, and representative Thai/CJK text;
- anchors, empty anchors, ignored content, comments, declarations and wrappers;
- NULL, empty and effectively empty descriptions;
- each unsupported tag/list attribute, stray unsupported end tags, typed
  reasons, standardized-row quarantine and systematic-quarantine failure;
- LinkedIn’s separate plaintext cleanup boundary;
- deterministic exact output for every normative branch.

Tests must assert exact strings including LF and indentation, or exact typed
failure/quarantine behavior.

## 13. Change policy

| Change class | Required handling |
| :--- | :--- |
| Matching-visible normalization change | Review this specification, update golden tests, review and revalidate the matching recipe, and rebuild both Silver products if adopted. |
| Stored-text-only change proven invariant after matching collapse | Review this specification, update golden tests, and perform rebuild/version handling as necessary; the matching recipe need not reopen. |
| No output-byte change | No matching-recipe change is required. |

A new meaningful HTML construct not safely covered here must surface for review.
Do not silently assign it extraction semantics.
