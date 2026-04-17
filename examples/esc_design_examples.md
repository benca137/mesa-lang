# `esc` Design Playground

These are realistic examples for comparing the different `esc` shapes we have
discussed.

Some of these are implemented today, and some are design-target examples only.
The point of this file is to make the tradeoffs visible in real code.

## Legend

- `Implemented`: supported by the current compiler
- `Design target`: discussed, but not implemented yet

---

## 1. Local scratch allocator, scalar result

This is the easy case. Nothing allocator-backed escapes.

Status: `Implemented`

```mesa
error ParseError { InvalidDigit, Empty }

fun parse_count(text: str) ParseError!int {
    let arena = ArenaAllocator(1024)

    with arena : .reset {
        if text.len == 0, return .Empty

        let var total = 0
        for ch in text {
            if ch < 48 or ch > 57, return .InvalidDigit
            total = total * 10 + (ch - 48)
        }

        total
    }
}
```

This reads well because `with` is just producing a plain value.

---

## 2. Local scratch allocator, escaped payload to caller-owned allocator

This is the most realistic success case for `esc`: build detailed data in a
scratch region, then promote it into a longer-lived allocator owned by the
caller.

Status: `Design target`

```mesa
error LoadError { MissingField(str), BadNumber(str) }

fun load_record(out: ArenaAllocator, line: str) LoadError!Record {
    let scratch = ArenaAllocator(4096)

    with out {
        with scratch : .free {
            let cols = split_csv(line)
            if cols.len < 3 {
                let msg = format_missing_field(scratch, "third column")
                return .MissingField(msg with out)
            }

            let qty = parse_int(cols[2]) catch {
                .Invalid => {
                    let msg = format_bad_number(scratch, cols[2])
                    return .BadNumber(msg with out)
                }
            }

            esc build_record(scratch, cols[0], cols[1], qty)
        }
    }
}
```

Why this example is useful:

- it shows explicit target promotion: `msg with out`
- it shows plain `esc` in tail position
- it avoids the orphaned-local-allocator problem because `out` is caller-owned

---

## 3. Same example, implicit “one allocator up” promotion

This is the same ownership story, but with the shorthand version of promotion.

Status: `Partially implemented`

```mesa
error LoadError { MissingField(str), BadNumber(str) }

fun load_record(out: ArenaAllocator, line: str) LoadError!Record {
    let scratch = ArenaAllocator(4096)

    with out {
        with scratch : .free {
            let cols = split_csv(line)
            if cols.len < 3 {
                let msg = format_missing_field(scratch, "third column")
                return .MissingField(esc msg)
            }

            let qty = parse_int(cols[2]) catch {
                .Invalid => {
                    let msg = format_bad_number(scratch, cols[2])
                    return .BadNumber(esc msg)
                }
            }

            esc build_record(scratch, cols[0], cols[1], qty)
        }
    }
}
```

This is the form that creates the current ambiguity:

- `esc msg` looks like value promotion
- `esc build_record(...)` also looks like control flow

The symmetry with `return` is nice, but the overloaded meaning is harder to
explain.

---

## 4. `try with ... { esc ... }` as an ordinary value expression

This is useful when the function itself is not returning the escaped value
directly, but wants to bind it locally.

Status: `Design target`

```mesa
fun build_name(out: ArenaAllocator, first: str, last: str) EscError!str {
    let scratch = ArenaAllocator(1024)

    let full: str = try with out {
        with scratch : .free {
            let joined = format_name(scratch, first, last)
            esc joined
        }
    }

    println(full)
    return full
}
```

This is a good readability test because it asks whether `esc` still feels
natural when it is not the final statement in the function.

---

## 5. A `with` block that handles local errors, then exports a value

This is where the interaction between `handle` and `esc` gets subtle.

Status: `Design target`

```mesa
error BuildError { MissingName, MissingZip }

fun compile_address(out: ArenaAllocator, form: FormData) BuildError!Address {
    let scratch = ArenaAllocator(2048)

    with out {
        with scratch : .free {
            let street = try form.require("street")
            let city   = try form.require("city")
            let zip    = try form.require("zip")

            esc .{
                street: copy_str(scratch, street),
                city: copy_str(scratch, city),
                zip: copy_str(scratch, zip),
            }
        } handle |e| {
            log_validation_failure(form.id, e)
            return e
        }
    }
}
```

This one is useful because it forces a decision:

- should `esc` be legal inside a `with` that has a local `handle`?
- if `esc` fails, should that bypass the local handle?
- does that make the block too magical?

---

## 6. Error payload built in scratch, promoted explicitly

This is the motivating error-payload case.

Status: `Design target`

```mesa
error ImportError {
    MissingColumn(str),
    BadValue(str),
}

fun import_row(out: ArenaAllocator, row: vec[str]) ImportError!Row {
    let scratch = ArenaAllocator(1024)

    with out {
        with scratch : .free {
            if row.len < 4 {
                let msg = format_missing_column(scratch, "price")
                return .MissingColumn(msg with out)
            }

            let price = parse_decimal(row[3]) catch {
                .Invalid => {
                    let msg = format_bad_value(scratch, row[3])
                    return .BadValue(msg with out)
                }
            }

            esc build_row(scratch, row, price)
        }
    }
}
```

This is the clearest argument for keeping explicit-target promotion in the
language even if an `esc` shorthand exists.

---

## 7. Function-style “export and return” shape

This is close to the symmetry you liked between `return` and `esc`.

Status: `Implemented for simple cases`

```mesa
fun duplicate_twice(out: ArenaAllocator, src: str) EscError!str {
    let scratch = ArenaAllocator(256)

    with out {
        with scratch : .free {
            let doubled = concat(scratch, src, src)
            esc doubled
        }
    }
}
```

This reads like:

- `return value` for ordinary values
- `esc value` for allocator-region values that must be promoted outward

This is elegant, but only if users can predict exactly which boundary it is
escaping from.

---

## 8. Explicit promotion without control-flow spelling

This version keeps promotion and return as separate ideas.

Status: `Design target`

```mesa
fun duplicate_twice(out: ArenaAllocator, src: str) EscError!str {
    let scratch = ArenaAllocator(256)

    with out {
        with scratch : .free {
            let doubled = concat(scratch, src, src)
            return doubled with out
        }
    }
}
```

This is more verbose, but semantically cleaner:

- `return` always means function exit
- `with out` always means allocator destination

It may be the least surprising form if `esc` starts to feel overloaded.

---

## 9. A local recovery that should not catch `esc` failure

This is the edge case we discussed around local handles versus export failure.

Status: `Design target`

```mesa
error ReportError { MissingSection, MissingSummary }

fun build_report(out: ArenaAllocator, doc: Document) EscError!Report {
    let scratch = ArenaAllocator(8192)

    with out {
        with scratch : .free {
            let title = try doc.get_title()
            let body = try doc.render_body(scratch)

            esc .{
                title: title,
                body: body,
            }
        } handle |e| {
            log_doc_problem(doc.id, e)
            return .MissingSummary
        }
    }
}
```

This example is useful because it exposes the split clearly:

- inner `try` failures belong to the local `handle`
- `esc` failure is about promotion, not document validation

If that distinction feels too subtle in real code, `esc` may need a different
surface form.

---

## 10. The ownership trap we should reject

This is the case that currently feels wrong.

Status: `Should become a compiler error`

```mesa
fun bad() EscError!vec[int] {
    let outer = ArenaAllocator(256)

    with outer {
        let inner = ArenaAllocator(64)
        with inner : .free {
            esc vec[1, 2, 3]
        }
    }
}
```

The `vec` survives `inner`, but it is now owned by `outer`, which is still only
a local allocator. The caller receives the value, but not the allocator that can
account for or free its storage.

This is a good test for whether the compiler is checking allocator ownership
rather than only cleanup-scope escape.

---

## Current takeaways

If we optimize for clarity:

- `return value` should stay plain function return
- `value with alloc` is the clearest explicit promotion form
- `esc value` is a good shorthand only if it means one thing consistently

If we optimize for ergonomics:

- `esc value` as “promote outward and return/propagate if needed” is pleasant
- but it mixes memory promotion and control flow in one keyword

The best next question is probably:

Should `esc` mean:

1. “promote outward” as an expression, and nothing more
2. “promote outward and implicitly return/propagate in tail position”
3. “promote outward and behave like `return` when used as a statement”

This file is meant to help answer that with real examples instead of tiny ones.
