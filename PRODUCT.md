# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One user: the owner of the project, a mechanical designer working alone on their own machine (Arch, Hyprland on Wayland). They open the workbench page while a Claude Code agent does the engineering in the project's code in a herdr pane in kitty. On the page they inspect parts, assemblies and simulations, comment, answer the agent's decision requests, keep the materials list current and check outputs. The agent drives the page through the `wbench` CLI.

The page normally runs fullscreen or maximized on a monitor of its own. The terminal with the agent is on a different screen.

## Product Purpose

The page lets the owner look at the same thing the agent is talking about and answer it there. Without it, decisions arrive as long chat messages, renders as file paths, and requirements as prose that gets lost. Success means a decision is read, compared against the model and answered on one screen, and a comment lands on the part it is about.

The page inspects, comments, decides and runs what-ifs. It never writes code; the agent applies every change.

## Positioning

The page is a lens and an inbox over the project repository, not a CAD tool. Geometry, simulation and numbers always come from the project's own code. The owner and the agent share saved views, and the agent never moves the owner's camera. Each view shows the measured facts it rests on: numbers decide, and pictures are only a glance.

## Operating Context

- The window is a `chromium --app` page served by the local hub (`wbench serve`). It talks to the hub over HTTP and WebSocket on 127.0.0.1.
- The UI uses Vite, React and TypeScript around the stock `three-cad-viewer` (hosted through `ocp-viewer-core`), with the workbench's own overlay layer for pins and markers.
- The current surfaces are a top bar and banner, the 3D viewer, and a side panel with the tabs Inbox, Item, Comment, Views, Materials and Outputs. Notices appear as toasts.
- Review items (comment, finding, decision, measurement, change) are the main objects. A decision lists corrections first, then frozen evidence tables, then questions with options, and can be answered in part.
- Sessions are long work sessions, not quick visits.

## Capabilities and Constraints

- The viewer must stay visible while the owner reads a decision with its evidence tables and options, and while they place and write a comment on a part. These panels must give tables and text enough room without squeezing the viewer out.
- Open: whether Materials and Outputs, which are list work, should take the whole window. The owner did not ask for that.
- This is a personal tool first. There is no installer, no onboarding and no other platform or browser to support for now.
- The page must not hide or reshape the stock viewer's own controls. Picking, measuring, sections and the tree stay upstream.

## Brand Commitments

None. In the owner's words, it "does not need to look nice". What matters is a good layout: everything gets enough space, but it stays compact enough to work with.

## Evidence on Hand

- `docs/plan.md`: the approved design.
- `ui/src`: the incumbent page.
- my-robot is the first real project, with live review items, a bom.toml and outputs.

## Product Principles

1. Layout serves the work: density over decoration, and every pixel of the screen earns its place.
2. The model and the question about it are visible together.
3. Numbers, units and sources are always legible. Tables are never truncated into ambiguity.
4. Show whose move it is and what is stale, so nothing waits silently.
5. The agent writes code and the page never does.
