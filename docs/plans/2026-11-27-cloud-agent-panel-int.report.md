# superlite build report

projection: fixed forecast saved at lint 2026-11-27T19:27:15

                         Projected    Actual  Delta
wall-clock (min)              10.2     216.5  +206.4 (+2030%)
agent cost (USD, +rework)      0.27      1.06  +0.79 (+294%)
task-minutes                  15.2     139.5  +124.2 (+814%)
agents                           3        23  +20 (+667%)
feature flow (cloud-agent-panel): two builds
  prototype   time n/a -> n/a min, cost $n/a -> $n/a
  integration time 10.2 -> 216.5 min, cost $0.27 -> $1.06
cost split: agents $1.06, rework $0.00, coordinator $11.67, total $12.73
concurrency: peak 3 at once (projected 2); critical path actual T7; projected T5 > T6 (10.2 min)
model mix: haiku 1 tasks $0.03, sonnet 6 tasks $1.03, coordinator opus $11.67
quality: tests 676 passed, 0 failed; first-time pass n/a; rework dispatches 0; open incidents 0

task  model      pred  actual  delta             cost
T7    sonnet      5.1     8.7  +3.6 (+72%)     $0.088
T6    sonnet      5.1     2.7  -2.4 (-47%)     $0.100
T5    sonnet      5.1     3.8  -1.3 (-25%)     $0.204
T0    haiku       n/a     2.9  n/a             $0.032
T1    sonnet      n/a     2.6  n/a             $0.332
(+2 smaller misses; `superlite-cli report --json` lists all)

agent cost table:
| agent | model | wall_min | tokens | usd |
| --- | --- | --- | --- | --- |
| a0952ac3df01e022d | haiku | 6.1666 | 506239 | 0.0141 |
| a3419241d8f0f4669 | sonnet | 2.7903 | 387694 | 0.3351 |
| a35bf35046e25d56b | sonnet | 121.4886 | 323899 | 0.3091 |
| a3ba9103a2b99f94e | sonnet | 29930189.0264 | 0 | 0.0 |
| a4013f6cfbbeef303 | sonnet | 29930350.1223 | 0 | 0.0 |
| a4c94557a96b1b554 | haiku | 0.9974 | 401309 | 0.013 |
| a510a3c62f6fd06d0 | sonnet | 29930225.3585 | 0 | 0.0 |
| a57f77e2edc586479 | sonnet | 2.9203 | 294022 | 0.127 |
| a6043d912a599580f | opus | 0.8142 | 263481 | 0.3189 |
| a63e8f0c49202d1e1 | sonnet | 29930217.847 | 0 | 0.0 |
| a65744402a5d6ee2b | haiku | 2.9057 | 1043759 | 0.0317 |
| a6727fcbac104f111 | sonnet | 29930221.4248 | 0 | 0.0 |
| a817ce85a1a88261c | sonnet | 29930439.1253 | 0 | 0.0 |
| a88db8e935f742b83 | sonnet | 29930421.0455 | 0 | 0.0 |
| a951b4ae52659478a | opus | 0.1831 | 14211 | 0.0589 |
| ab633b551392c1bf9 | opus | 0.7374 | 240582 | 0.2983 |
| ab747fe3c8b7c1b24 | sonnet | 29930415.9395 | 0 | 0.0 |
| ac25db28b1d6c8140 | sonnet | 2.5979 | 161852 | 0.0736 |
| ac8d5f4c449db1808 | sonnet | 29930428.2832 | 0 | 0.0 |
| acd7cca9e59cad50f | sonnet | 3.8906 | 353908 | 0.1776 |
| ad7ad2a7a8cb9c25c | sonnet | 2.6119 | 517120 | 0.3318 |
| aebdd66e92ccbc6ba | sonnet | 29930236.2397 | 0 | 0.0 |
| afc28caf5d9fdad8e | sonnet | 29930239.4555 | 0 | 0.0 |
| totals |  | 329233531.9717 | 4508076 | 2.0891 |
cost source: est

trend (last 6 runs): first-time pass n/a -> n/a -> n/a -> n/a -> n/a -> n/a; findings 4 -> 6 -> 7 -> 0 -> 0 -> 5; rework+fixer cost 0% -> 3% -> 0% -> 0% -> 0% -> 0%
lessons: 19 cached, 0 proven
takeaway: ran 2030% slower than projected; cost 294% over; biggest miss T7
