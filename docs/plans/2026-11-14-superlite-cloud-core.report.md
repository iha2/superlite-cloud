# superlite build report

projection: fixed forecast saved at lint 2026-11-14T15:17:07 (plan edited since)

                         Projected    Actual  Delta
wall-clock (min)               6.5      85.7  +79.3 (+1220%)
agent cost (USD, +rework)      1.98      2.34  +0.37 (+18%)
task-minutes                  32.7     369.9  +337.1 (+1030%)
agents                          16        36  +20 (+125%)
cost split: agents $2.27, rework $0.07, coordinator $14.37, total $16.71
concurrency: peak 5 at once (projected 10); critical path actual T8; projected T1 > T4 > T16 (6.5 min)
model mix: haiku 2 tasks $0.04, sonnet 12 tasks $2.31, coordinator opus $14.37, coordinator sonnet $0.00
quality: tests 186 passed, 0 failed; first-time pass n/a; rework dispatches 1; open incidents 119

task  model      pred  actual  delta             cost
T8    sonnet      2.0    78.6  +76.5 (+3761%)  $0.440
T4    sonnet      2.1    71.8  +69.8 (+3361%)  $0.286
T13   sonnet      2.1    68.1  +66.0 (+3213%)  $0.245
T10   sonnet      2.7    67.6  +64.9 (+2442%)  $0.234
T12   sonnet      3.1    67.4  +64.4 (+2100%)  $0.136
(+9 smaller misses; `superlite-cli report --json` lists all)

agent cost table:
| agent | model | wall_min | tokens | usd |
| --- | --- | --- | --- | --- |
| a0c878fc4361a1f1f | sonnet | 0.728 | 106130 | 0.0776 |
| a0d1b9e83181f539b | opus | 0.5204 | 31890 | 0.1795 |
| a0e188152677c7d90 | sonnet | 0.5084 | 77193 | 0.0685 |
| a113be612df112108 | sonnet | 0.553 | 65854 | 0.0625 |
| a1cb159a4d0ffd9d2 | sonnet | 29911449.8529 | 0 | 0.0 |
| a26225cbe7a9f5bc2 | sonnet | 29911466.3585 | 0 | 0.0 |
| a29e783cd8b5e529b | sonnet | 29911532.0175 | 0 | 0.0 |
| a3a1183bcb71d1eed | sonnet | 29911535.9888 | 0 | 0.0 |
| a4a01c0efb7db5f3a | haiku | 29911539.8895 | 857049 | 0.0357 |
| a4b7475d68222b7c6 | sonnet | 29911547.8239 | 0 | 0.0 |
| a4cc1bffa7bd236c2 | sonnet | 29911451.6488 | 0 | 0.0 |
| a5957724f436a61de | sonnet | 0.6957 | 117261 | 0.0784 |
| a59bf0c7ea1e3f6ae | sonnet | 0.6032 | 93458 | 0.0741 |
| a5a9cd10d4c590868 | sonnet | 29911461.2477 | 0 | 0.0 |
| a5cd9f194d004bfe4 | sonnet | 0.504 | 86372 | 0.0619 |
| a66b1ef01c86e595c | sonnet | 67.4514 | 156925 | 0.1364 |
| a6b96709407a85e3e | sonnet | 2.9998 | 507755 | 0.2155 |
| a7050e1b13e5e9a43 | sonnet | 29911463.6853 | 0 | 0.0 |
| a7585da6feed35e79 | sonnet | 78.0325 | 479941 | 0.3426 |
| a7af03e8905bcfaa3 | sonnet | 0.4875 | 89518 | 0.0497 |
| a8019acadfdd0f749 | sonnet | 0.6694 | 121435 | 0.0857 |
| a9451dc208238365a | sonnet | 71.3606 | 342705 | 0.2171 |
| aa886ae8db37d2fb1 | sonnet | 0.4608 | 46890 | 0.0614 |
| aaf9bbb7aafe16aa3 | sonnet | 29911569.0017 | 0 | 0.0 |
| ab51d6709cb562236 | sonnet | 0.4491 | 71249 | 0.0751 |
| abb0e6deabc11e678 | sonnet | 29911543.4027 | 0 | 0.0 |
| ad3db2a4a869c21b5 | sonnet | 72.5291 | 909871 | 0.4318 |
| ad666902738cc7550 | sonnet | 29911465.7903 | 218037 | 0.1507 |
| ad9c8216ed7c94f8e | sonnet | 29911533.2438 | 363058 | 0.323 |
| ae221e407dea3c2ab | sonnet | 0.5805 | 126595 | 0.097 |
| ae2810ef84104a135 | sonnet | 0.5109 | 57039 | 0.0657 |
| ae834fa18a8f73db6 | sonnet | 29911547.1785 | 0 | 0.0 |
| af883b5564fa7160f | sonnet | 0.3489 | 37755 | 0.0561 |
| af99a9d3c616f6b37 | sonnet | 29911442.9071 | 0 | 0.0 |
| afc7787b7c7b63cd0 | sonnet | 29911566.1256 | 0 | 0.0 |
| afdf2d2cb340bae9f | sonnet | 29911465.7634 | 254811 | 0.1355 |
| totals |  | 508495881.9192 | 5218791 | 3.0815 |
cost source: est

trend (last 1 run): first-time pass n/a; findings 0; rework+fixer cost 3%
lessons: 2 cached, 0 proven
takeaway: ran 1220% slower than projected; cost 18% over; biggest miss T8
