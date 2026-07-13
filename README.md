[![Review Assignment Due Date](https://classroom.github.com/assets/deadline-readme-button-22041afd0340ce965d47ae6ef1cefeee28c7c493a6346c4f15d667ab976d596c.svg)](https://classroom.github.com/a/D741gj53)
[![Open in Codespaces](https://classroom.github.com/assets/launch-codespace-2972f46106e565e64193e422d61a12cf1da4916b45550586e14ef0a7c637dd04.svg)](https://classroom.github.com/open-in-codespaces?assignment_repo_id=24234332)
# Final Project - Build Something Worth Showing Off

## For Students

### The Project
This is the capstone of camp. You will design and build a program of your own, polish it, document it, and demo it at the **showcase**. Done well, this is a project you can put on a resume or point to in a college application. Three tracks:

- **Track A: Image processing.** Open an image and transform it with a function you write yourself: brightness adjustment, a color filter overlay, grayscale, pixelate, or your own effect. The Pillow library is preinstalled.
- **Track B: Adventure game.** A text adventure with real choices, an inventory, and `random` surprises: treasure, traps, rooms, etc.
- **Track C: Your own idea.** A bigger game (pygame and turtle both work here), a useful tool, a simulation. Pitch it to your instructor first.

Open `final_project.py` for the requirements, starter guidance for each track, and a planning section. Fill out **PROJECT.md** as you go; it is part of the grade and it is the write-up a stranger would read to understand your work.

### What Makes It Showcase-Worthy
The autograder checks the basics: code organized into **functions**, real **logic** (decisions plus loops), **substance** (bigger than the mini-project), and a completed **PROJECT.md**. The humans at showcase judge the rest: creativity, polish, and how well you can explain it.

### Tools You Have
- Python 3.11 with **Pillow** and **pygame** preinstalled, plus `turtle` and everything from Modules 1-6
- The virtual desktop for anything graphical: run your program, open the **PORTS** tab, click port **6080**, Connect, password `vscode`
- **Live Share** for working together in real time
- Need pixel art or sprites? Draw them free in your browser at [piskelapp.com](https://www.piskelapp.com), export the PNG, and drag it into the file explorer
- Split your code into multiple `.py` files if it gets big; the grader reads all of them

### Submitting
- Commit and push like always: **Source Control** icon, message, **Commit**, then **Sync Changes**
- Push early and often; the autograder tells you which showcase basics you still need
- Your last push before showcase is what gets graded, so make sure the final version is synced!

---

## For Instructors

### How This Repo Works
- `final_project.py` — the student-facing brief: requirements, three tracks, planning prompts (students may add more .py files)
- `PROJECT.md` — the write-up template students must complete (title, description, how to run, how it works, credits)
- `tests/test_final_project.py` — autograder; see the note below
- `.github/workflows/grade.yml` — GitHub Action that runs on every push to `main` and posts results as a commit comment
- `.devcontainer/devcontainer.json` — Codespaces environment (Python 3.11, Pillow + pygame preinstalled, desktop-lite VNC on port 6080, Live Share, AI features disabled)

### A Note on Grading an Open-Ended Capstone
Student projects may block on input(), loop forever, or open a window, so the autograder
**never runs the code**. It parses every project `.py` file with `ast` and checks
showcase-readiness basics: organized code (3+ functions, a class, or multiple .py
files), both conditionals and loops present,
at least 40 statements total (a substance floor, roughly "bigger than the mini-project"),
and a PROJECT.md with all placeholders replaced. It is an effort-and-structure check.
The real evaluation happens at showcase: have each student or team demo live, explain
their code, and answer one "why did you build it this way" question. PROJECT.md doubles
as their portfolio write-up, so give feedback on the writing too.

### Setting Up GitHub Classroom
1. Go to [classroom.github.com](https://classroom.github.com) and create/open your classroom
2. Click **New assignment**, choose **Individual or Group** to match your showcase format (this cannot be changed after creation), and use this repo as the starter code
3. Set visibility to **Private**
4. Add an autograding test (**Run command**: `python tests/test_final_project.py`); weight it low and put most of the grade on the showcase demo and PROJECT.md
5. Consider protecting `tests/` and `.github/` via **protected file paths**
6. Share the generated invite link with students
