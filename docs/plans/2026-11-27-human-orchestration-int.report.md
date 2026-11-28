# superlite build report

projection: fixed forecast saved at lint 2026-11-27T19:16:08 (plan edited since)

                         Projected    Actual  Delta
wall-clock (min)              23.8      44.1  +20.4 (+86%)
agent cost (USD, +rework)      0.80      2.05  +1.25 (+156%)
task-minutes                  66.3      39.1  -27.2 (-41%)
agents                          10        14  +4 (+40%)
cost split: agents $1.96, rework $0.09, coordinator $24.07, total $26.12
concurrency: peak 5 at once (projected 5); critical path actual T5; projected T1 > T4 > T10 (23.8 min)
model mix: haiku 1 tasks $0.01, sonnet 8 tasks $2.04, coordinator opus $24.07, coordinator sonnet $0.00
quality: tests 692 passed, 0 failed; first-time pass n/a; rework dispatches 1; open incidents 40

task  model      pred  actual  delta             cost
T10   sonnet     11.6     0.3  -11.3 (-97%)    $0.057
T5    sonnet      6.1    16.7  +10.7 (+176%)   $0.570
T2    sonnet      6.1     1.0  -5.1 (-83%)     $0.113
T4    sonnet      6.1     1.5  -4.6 (-75%)     $0.094
T9    sonnet      6.1     4.4  -1.7 (-28%)     $0.238
(+4 smaller misses; `superlite-cli report --json` lists all)

agent cost table:
| agent | model | wall_min | tokens | usd |
| --- | --- | --- | --- | --- |
| a108d49454e915121 | sonnet | 5.7865 | 730597 | 0.3721 |
| a30da8efd7ac4e9b5 | sonnet | 6.6311 | 985597 | 0.3507 |
| a342ca48f47e46f81 | sonnet | 29930424.3032 | 0 | 0.0 |
| a3edd303530c98291 | opus | 0.3891 | 26330 | 0.139 |
| a5934613ece893578 | sonnet | 1.5149 | 163891 | 0.0945 |
| a61c08324c93bbc25 | sonnet | 29930417.3737 | 0 | 0.0 |
| ab1c50b8367065cbb | haiku | 3.9531 | 243955 | 0.0088 |
| ab408bae7d30a130b | sonnet | 29930420.9524 | 0 | 0.0 |
| ab4d36154d3a132aa | sonnet | 10.9696 | 257533 | 0.1084 |
| abd6b30e4f4eb72fe | sonnet | 29930429.666 | 0 | 0.0 |
| acc09cdb506aeb3ba | sonnet | 7.4324 | 1516614 | 0.5412 |
| ad780233ca3040fca | sonnet | 0.4522 | 130885 | 0.0783 |
| ae093d007c1e1d776 | sonnet | 4.4582 | 924014 | 0.4098 |
| af74c41e7e1bc4ec8 | sonnet | 27.6887 | 204333 | 0.0896 |
| totals |  | 119721761.5711 | 5183749 | 2.1924 |
cost source: est

trend (last 6 runs): first-time pass n/a -> n/a -> n/a -> n/a -> n/a -> n/a; findings 4 -> 6 -> 7 -> 0 -> 0 -> 5; rework+fixer cost 0% -> 3% -> 0% -> 0% -> 0% -> 4%
lessons: 19 cached, 0 proven
takeaway: ran 86% slower than projected; cost 156% over; biggest miss T10
