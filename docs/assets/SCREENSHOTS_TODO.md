# Screenshots checklist for the dashboard

The CLI captures are committed (`docs/assets/*.txt`). The web dashboard
needs PNG screenshots taken from the browser. Here's a 2-min recipe :

## 1. Launch the dashboard

```bash
ship1000x dashboard
# → opens http://localhost:10000 automatically
```

## 2. Captures to take (4 PNG files)

Save each to `docs/assets/` with these exact names so README references work :

| Page | Filename | What to show |
|---|---|---|
| Overview (light mode) | `dashboard-overview-light.png` | Top of `/` showing 6 metric cards + trend chart |
| Overview (dark mode) | `dashboard-overview-dark.png` | Same page after clicking the moon icon |
| Projects | `dashboard-projects.png` | `/projects` with the table populated |
| Trust breakdown | `dashboard-trust.png` | Lower part of `/` showing per-source bars |

Recommended : 1440×900 viewport (Mac default), Chrome Cmd+Shift+5 area
capture, no browser chrome (UI only).

## 3. Add to README

Once captured, replace the "📸 Screenshots coming" block in README.md with :

```markdown
| Light mode | Dark mode |
|---|---|
| ![](docs/assets/dashboard-overview-light.png) | ![](docs/assets/dashboard-overview-dark.png) |

Projects view : ![](docs/assets/dashboard-projects.png)
Trust by source : ![](docs/assets/dashboard-trust.png)
```

Then commit + push :

```bash
git add docs/assets/dashboard-*.png README.md
git commit -m "docs: add dashboard screenshots"
git push
```

## Optional : animated GIF

For LinkedIn/HN demo, a short GIF (Quicktime → screen recording → convert
to GIF via ffmpeg or Cleanshot) showing the dashboard in action is
high-impact. ~10 seconds is enough.
