# csvkit (agit demo workspace)

A two-function CSV helper with one real bug: `parse_row` splits on every comma,
so a quoted field like `"Powai, Hiranandani"` is torn in half.

`agit demo` points an agent at this directory with a definition of done:

    python3 -m unittest discover -q -p "test_*.py" .

Afterwards, the interesting part is not the fix. It is that you can ask the
ledger who wrote each line and why:

    agit log
    agit blame csvkit.py
    agit why csvkit.py 24
