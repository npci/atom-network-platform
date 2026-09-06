
Then for each file, use these exact markers — note the `[repo-label]` prefix
which routes the file to the correct repository:
```
<<FILE: [{{REPO_LABEL_EXAMPLE_CORE}}] {{REPO_PATH_EXAMPLE_CORE}}>>
<complete file content — the entire file, modified>
<<END_FILE>>

<<FILE: [{{REPO_LABEL_EXAMPLE_APP}}] {{REPO_PATH_EXAMPLE_APP}}>>
<complete file content — the entire file, modified>
<<END_FILE>>
```

If a file already exists in the file tree above, use the SAME repo it currently
lives in (look at which `## Repo:` header the path is listed under). For NEW
files, choose the repo based on what kind of code it is — typically: shared
DTOs / XSDs / library utilities go to the core/library repo; controllers /
handlers / services / integration code go to the application repo.

