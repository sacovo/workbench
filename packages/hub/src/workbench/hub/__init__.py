"""The hub: `wb serve`'s local app server for one project.

    app.py           HTTP and WebSocket routes behind the token, Origin and Host guard
    hub.py           kernel generations, pages, the shown scene, picks, comments from the page
    kernel.py        one kernel process and its JSON-lines protocol
    review_watch.py  what the page shows of review/, pushed when the files change
    launch.py        the owner's chromium --app window
    server.py        the foreground server (`wb serve`)
    static/          the page, built from ui/ (no Node needed at runtime)

Nothing here imports CAD code: the kernel runs in the project's environment.
"""
