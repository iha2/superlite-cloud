# superlite build report

projection: fixed forecast saved at lint 2026-11-25T17:56:08

                         Projected    Actual  Delta
wall-clock (min)              18.8      18.8  +0.0 (+0%)
agent cost (USD, +rework)      0.86      1.53  +0.67 (+78%)
task-minutes                  37.6      22.1  -15.5 (-41%)
agents                           6        10  +4 (+67%)
cost split: agents $1.48, rework $0.05, coordinator $25.24, total $26.77
concurrency: peak 2 at once (projected 3); critical path actual T2 T3; projected T1 > T3 > T6 (18.8 min)
model mix: sonnet 5 tasks $1.48, unknown 1 tasks $0.05, coordinator opus $25.24
quality: tests 10 passed, 0 failed; first-time pass n/a; rework dispatches 1; open incidents 28

task  model      pred  actual  delta             cost
T5    ?           6.3     0.0  -6.3 (-100%)    $0.047
T1    sonnet      6.3     0.9  -5.3 (-85%)     $0.083
T3    sonnet      6.3    10.9  +4.7 (+74%)     $0.505
T4    sonnet      6.3     3.1  -3.2 (-51%)     $0.397
T6    sonnet      6.3     3.3  -2.9 (-47%)     $0.288
(+1 smaller misses; `superlite-cli report --json` lists all)

agent cost table:
| agent | model | wall_min | tokens | usd |
| --- | --- | --- | --- | --- |
| a03988f3b613f7361 | sonnet | 0.374 | 71079 | 0.0471 |
| a03b7c4ada331fa66 | sonnet | 0.9227 | 158508 | 0.0939 |
| a1c2b72f033d56126 | sonnet | 5.8953 | 771538 | 0.3426 |
| a2122ec63d919b9b5 | sonnet | 29927458.6385 | 0 | 0.0 |
| a228e2f1edccf7572 | opus | 0.5581 | 28569 | 0.1639 |
| a3ad0e9cf55f54168 | sonnet | 5.0772 | 385089 | 0.1623 |
| a49c5cba72eaf56f5 | sonnet | 3.9269 | 453679 | 0.209 |
| a88ec6db3bb53ad81 | sonnet | 0.9615 | 127435 | 0.0833 |
| abac7909dad48afbd | sonnet | 10.1411 | 1801151 | 0.6851 |
| af1fa02eb1b9e9f8d | sonnet | 29927468.5788 | 0 | 0.0 |
| totals |  | 59854955.0741 | 3797048 | 1.7872 |
cost source: est

trend (last 2 runs): first-time pass n/a -> n/a; findings 4 -> 6; rework+fixer cost 0% -> 3%
lessons: 14 cached, 0 proven
takeaway: ran on projection; cost 78% over; biggest miss T5
