# AI · Chapter 3: Solving problems by searching

- Uninformed search: BFS is complete and optimal for unit step costs; DFS uses little memory but is not complete.
- Uniform-cost search expands the node with the lowest path cost g(n).
- A* search expands the node with the lowest f(n) = g(n) + h(n); it is optimal when h is admissible.
- Greedy best-first search uses only h(n): fast, but neither complete nor optimal.
- Exam tip from Dr. Huda: be able to trace A* by hand on a small graph.
