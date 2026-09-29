const state = { user: null, guest: false, authMode: "login", page: "overview", events: [], overview: null, assignments: [], judgeSummary: null, projects: [], myProjects: [], activeProject: null, activeGalleryProject: null, criteria: [], team: null, results: [], publicResults: null, pairwise: null, pairwiseOrganizer: null, normalization: {}, invitations: [], webhooks: [], certificates: [], judgeRecords: [], judges: [], awards: [], moderationComments: [], auditLogs: [], adminUsers: [], galleryPolicy: {}, toast: "" };
const app = document.querySelector("#app");
const API_BASE = "/api";

async function api(path, options = {}) {
  const normalizedPath = API_BASE && path.startsWith("/api/") ? path.slice(4) : path;
  const response = await fetch(`${API_BASE}${normalizedPath}`, { credentials: "same-origin", headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.detail || "Something went wrong");
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("json") ? response.json() : response.text();
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[char]));
}
function initials(name) { return name.split(" ").map((part) => part[0]).slice(0, 2).join("").toUpperCase(); }
function formatDate(value) { return value ? new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "—"; }
function inputDate(value) { return value ? String(value).replace("Z", "").slice(0, 16) : ""; }
function notify(message) { state.toast = message; render(); setTimeout(() => { state.toast = ""; render(); }, 2600); }
function isOrganizer() { return ["organizer", "admin"].includes(state.user?.role); }

async function boot() {
  const session = await api("/session");
  state.user = session.user;
  if (!state.user) return renderLogin();
  await loadPage("overview");
}

async function loadPage(page) {
  state.page = page;
  try {
    const [events, projects, publicResults] = await Promise.all([api("/api/events"), api("/api/gallery"), api("/api/results")]);
    state.events = events.items; state.projects = projects.items; state.galleryPolicy = projects.voting || {};
    state.publicResults = publicResults;
    if (isOrganizer()) state.overview = await api("/api/organizer/overview");
    if (isOrganizer()) {
      const resultData = await api("/api/organizer/results");
      state.results = resultData.items;
      state.normalization = resultData.normalization || {};
    }
    if (isOrganizer()) state.assignments = (await api("/api/judge/assignments")).items;
    if (isOrganizer()) state.invitations = (await api("/api/organizer/judge-invitations")).items;
    if (state.user.role === "judge") {
      const judgeQueue = await api("/api/judge/assignments");
      state.assignments = judgeQueue.items;
      state.judgeSummary = judgeQueue.summary || null;
    }
    if (state.user.role === "judge") state.pairwise = await api("/api/judge/pairwise");
    if (state.user.role === "participant") {
      state.team = await api("/api/me/team");
      state.myProjects = (await api("/api/me/projects")).items;
    }
    if (isOrganizer()) {
      state.webhooks = (await api("/api/organizer/webhooks")).items;
      state.certificates = (await api("/api/organizer/certificates")).items;
      state.judgeRecords = (await api("/api/organizer/judge-records")).items;
      state.judges = (await api("/api/organizer/judges")).items;
      state.awards = (await api(`/api/organizer/awards?event_id=${state.events[0]?.id || ""}`)).items;
      state.pairwiseOrganizer = await api(`/api/organizer/pairwise?event_id=${state.events[0]?.id || ""}`);
      state.moderationComments = (await api(`/api/organizer/comments?event_id=${state.events[0]?.id || ""}`)).items;
      state.auditLogs = (await api("/api/organizer/audit-logs")).items;
      if (state.user.role === "admin") state.adminUsers = (await api("/api/admin/users")).items;
    }
    render();
  } catch (error) { render(); notify(error.message); }
}

function renderLogin() {
  const registering = state.authMode === "register";
  app.innerHTML = `
    <main class="login-screen">
      <section class="login-card">
        <div class="login-intro">
          <div class="brand"><span>H!</span> HACKNIGHT</div>
          <div class="eyebrow">SELF-HOSTED JUDGING CONTROL ROOM</div>
          <h1>Ship the event.<br><span>Defend the score.</span></h1>
          <p>A local-first platform for submissions, assignments, weighted rubrics, and public results. Built for the people who actually have to run the hackathon.</p>
          <div class="login-facts">
            <div class="login-fact">Backend-enforced judge isolation</div>
            <div class="login-fact">Seeded data. No cloud account required.</div>
            <div class="login-fact">One command to run the whole thing</div>
          </div>
        </div>
        <form class="login-form" id="${registering ? "register-form" : "login-form"}">
          <h2>${registering ? "Create your participant account" : "Enter the control room"}</h2>
          <p>${registering ? "Create a local account to join a team, submit a project, and vote." : "Use a seeded account or your own local credentials."}</p>
          ${registering ? `<div class="form-field"><label>Your name</label><input class="input" name="name" autocomplete="name" required /></div>` : ""}
          <div class="form-field" style="margin-top:${registering ? "14px" : "0"}"><label>Email</label><input class="input" name="email" type="email" autocomplete="username" value="${registering ? "" : "organizer@hacknight.local"}" required /></div>
          <div class="form-field" style="margin-top:14px"><label>Password</label><input class="input" name="password" type="password" autocomplete="${registering ? "new-password" : "current-password"}" value="${registering ? "" : "organizer"}" minlength="${registering ? "8" : "1"}" required /></div>
          <div id="login-error" class="error"></div>
          <button class="btn primary" style="width:100%; margin-top:18px">${registering ? "Create account" : "Sign in"}</button>
           <button type="button" class="btn ghost" id="visitor-mode" style="width:100%; margin-top:10px">Browse gallery as visitor</button>
          ${!registering ? `<div class="auth-switch">New here? <button type="button" id="show-register">Create a participant account</button></div>` : `<div class="auth-switch">Already registered? <button type="button" id="show-login">Sign in</button></div>`}
          ${!registering ? `<div class="quick-login">
            <p>Seeded demo accounts</p>
            <button type="button" data-email="organizer@hacknight.local" data-password="organizer">Maya Chen · organizer</button>
             <button type="button" data-email="diego.herrera@example.org" data-password="judge">Diego Herrera · fixture judge</button>
             <button type="button" data-email="ada40@example.org" data-password="participant">Ada · fixture participant</button>
          </div>` : ""}
        </form>
      </section>
    </main>`;
  document.querySelectorAll(".quick-login button").forEach((button) => button.addEventListener("click", () => {
    document.querySelector('[name="email"]').value = button.dataset.email;
    document.querySelector('[name="password"]').value = button.dataset.password;
  }));
   document.querySelector("#visitor-mode").addEventListener("click", () => {
     state.guest = true;
     state.user = { name: "Visitor", email: "", role: "visitor" };
     loadPage("gallery");
   });
   document.querySelector("#show-register")?.addEventListener("click", () => { state.authMode = "register"; renderLogin(); });
   document.querySelector("#show-login")?.addEventListener("click", () => { state.authMode = "login"; renderLogin(); });
   document.querySelector("#login-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try { const result = await api("/auth/login", { method: "POST", body: JSON.stringify({ email: form.get("email"), password: form.get("password") }) }); state.user = result.user; await loadPage("overview"); }
    catch (error) { document.querySelector("#login-error").textContent = error.message; }
  });
   document.querySelector("#register-form")?.addEventListener("submit", async (event) => {
     event.preventDefault();
     const form = new FormData(event.currentTarget);
     try {
       const result = await api("/auth/register", { method: "POST", body: JSON.stringify({ name: form.get("name"), email: form.get("email"), password: form.get("password") }) });
       state.user = result.user; state.authMode = "login"; await loadPage("overview");
     } catch (error) { document.querySelector("#login-error").textContent = error.message; }
   });
}

function shell(content, title, subtitle) {
  const nav = [
    ["overview", "Overview"],
    ["gallery", "Public gallery"],
    ["results", "Results"],
    ...(state.user.role === "participant" ? [["submit", "My submission"]] : []),
    ...(state.user.role === "judge" ? [["judge", "Judge queue"]] : []),
    ...(isOrganizer() ? [["organizer", "Organizer desk"]] : []),
  ];
  app.innerHTML = `
    <div class="app-shell">
      <aside class="rail">
        <div class="brand"><span>H!</span> HACKNIGHT</div>
        <div class="brand-sub">SELF-HOSTED EVENT OS</div>
        <div class="event-chip"><div class="eyebrow">ACTIVE EVENT</div><strong>${esc(state.events[0]?.name || "Hacknight Zero")}</strong><small>DEADLINE ${formatDate(state.events[0]?.deadline)}</small></div>
        <nav class="nav">${nav.map(([key, label]) => `<button class="${state.page === key ? "active" : ""}" data-page="${key}">${label}</button>`).join("")}</nav>
        <div class="rail-footer"><div class="user-row"><div class="avatar">${initials(state.user.name)}</div><div><strong>${esc(state.user.name)}</strong><small>${state.user.role.toUpperCase()}</small></div></div><button class="logout" id="logout">Sign out</button></div>
      </aside>
      <main class="main">
        <header class="topbar"><div><h1>${title}</h1><p>${subtitle}</p></div><div class="top-actions"><span class="badge cyan">LOCAL / ONLINE</span><button class="btn small" data-page="gallery">View gallery</button></div></header>
        <div class="content">${content}</div>
      </main>
    </div>
    ${state.toast ? `<div class="toast">${esc(state.toast)}</div>` : ""}`;
  document.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => loadPage(button.dataset.page)));
   document.querySelector("#logout").addEventListener("click", async () => {
     if (!state.guest) await api("/api/auth/logout", { method: "POST" });
     state.user = null; state.guest = false; renderLogin();
   });
}

function overviewPage() {
  const event = state.events[0] || {};
  const counts = state.overview?.counts || { projects: state.projects.length, submitted: state.projects.length, judges: 2, assignments: 4, scores: 0, criteria: 4 };
  const scoredPercent = counts.assignments ? Math.round((counts.scores / (counts.assignments * counts.criteria)) * 100) : 0;
  return `
    <div class="grid metrics">
      <div class="metric"><div class="label">Submissions</div><div class="value">${counts.submitted}</div><div class="trend">of ${counts.projects} projects</div></div>
      <div class="metric"><div class="label">Judge coverage</div><div class="value">${counts.assignments}</div><div class="trend">${counts.judges} active judges</div></div>
      <div class="metric"><div class="label">Scoring complete</div><div class="value">${scoredPercent}%</div><div class="trend">${counts.scores} criterion scores</div></div>
      <div class="metric"><div class="label">Deadline</div><div class="value">${formatDate(event.deadline)}</div><div class="trend">UTC · enforced</div></div>
    </div>
    <div class="section-head"><div><h2>${esc(event.name || "Hacknight Zero")}</h2><p>${esc(event.tagline || "")}</p></div><span class="badge pink">T1 + T2 ACTIVE</span></div>
    <div class="grid two-col">
      <section class="panel"><div class="eyebrow">JUDGING PROGRESS</div><h2 style="margin:12px 0 0">Every score has a paper trail.</h2><div class="progress-line"><span style="width:${scoredPercent}%"></span></div><div class="progress-meta"><span>${counts.scores} / ${counts.assignments * counts.criteria || 1} criterion scores recorded</span><span>${scoredPercent}%</span></div><p class="muted" style="font-size:12px;line-height:1.6;margin-top:22px">Judges only see projects assigned to them. Scores are stored per assignment, normalized at the event level, and exportable as CSV by organizers.</p></section>
      <section class="panel"><div class="eyebrow">EVENT SIGNAL</div><div class="activity" style="margin-top:18px">${(state.overview?.recent || [{ action: "seed", entity: "event", entity_id: "1", user_name: "System", created_at: new Date().toISOString() }]).slice(0,5).map((item) => `<div class="activity-item"><span class="activity-dot"></span><div><strong>${esc(item.user_name || "System")} ${esc(item.action)}d ${esc(item.entity)}</strong><div class="activity-time">${formatDate(item.created_at)} · #${esc(item.entity_id)}</div></div></div>`).join("")}</div></section>
    </div>
    <div class="section-head"><div><h2>Recent submissions</h2><p>The public gallery only shows submitted projects.</p></div><button class="btn small" data-page="gallery">Open all</button></div>
    <section class="panel"><table class="table"><thead><tr><th>Project</th><th>Track</th><th>Status</th><th>Community</th><th>Judging</th></tr></thead><tbody>${state.projects.slice(0,5).map((project) => `<tr><td><span class="project-title">${esc(project.title)}</span><span class="project-sub">${esc(project.team_name)}</span></td><td><span class="badge" style="color:${project.track_color};border-color:${project.track_color}">${esc(project.track_name)}</span></td><td><span class="badge ${project.status === "submitted" ? "cyan" : ""}">${project.status.toUpperCase()}</span></td><td class="mono">${project.votes} votes</td><td class="mono">${project.judge_average ? Number(project.judge_average).toFixed(1) : "—"} / 5</td></tr>`).join("")}</tbody></table></section>`;
}

function galleryPage() {
  const policy = state.galleryPolicy || {};
  const accessLabel = policy.access === "open" ? "OPEN LINK" : policy.access === "email" ? "EMAIL GATED" : "AUTHENTICATED";
  const votingLabel = policy.window_closed ? "VOTING CLOSED" : policy.can_vote ? `${accessLabel} · ${policy.mode.replaceAll("_", " ").toUpperCase()}` : `${accessLabel} · SIGN IN TO VOTE`;
  return `
    <div class="section-head"><div><h2>Public project gallery</h2><p>Explore what builders shipped. Voting is randomized and separate from judge scores.</p></div><div class="top-actions"><span class="badge cyan">${state.projects.length} PUBLISHED</span><span class="badge ${policy.can_vote ? "pink" : "yellow"}">${votingLabel}</span></div></div>
    ${policy.results_revealed ? "" : `<div class="notice">Community and judge results are hidden while the voting window is open. Organizers can still monitor progress.</div>`}
    <div class="filters"><input id="gallery-search" class="input" placeholder="Search projects, teams, technologies, or ideas…" /><select id="gallery-track" class="select" style="max-width:220px"><option value="">All tracks</option>${[...new Map(state.projects.map((p) => [p.track_name, p.track_name])).values()].map((track) => `<option>${esc(track)}</option>`).join("")}</select><select id="gallery-tech" class="select" style="max-width:220px"><option value="">All technologies</option>${[...new Set(state.projects.flatMap((p) => p.tech_tags || []))].sort().map((tag) => `<option>${esc(tag)}</option>`).join("")}</select></div>
    <div class="gallery-grid" id="gallery-grid">${galleryCards(state.projects)}</div>`;
}
function galleryCards(projects) {
  const policy = state.galleryPolicy || {};
  return projects.length ? projects.map((project) => {
    const scoreLabel = policy.results_revealed ? `${project.votes ?? 0} votes · ${project.judge_average ? Number(project.judge_average).toFixed(1) : "—"}/5` : "Results hidden";
    const emailVote = policy.access === "email" && state.guest;
    const voteControl = policy.can_vote || emailVote
      ? `${policy.mode === "quadratic" ? `<input class="vote-quantity" data-vote-quantity="${project.id}" type="number" min="1" max="4" value="1" aria-label="Votes for ${esc(project.title)}" />` : ""}<button class="btn small" data-vote="${project.id}" ${emailVote ? "data-email-vote=\"true\"" : ""}>${emailVote ? "Enter email to vote" : "Vote"}</button>`
      : `<button class="btn small" disabled>${policy.window_closed ? "Voting closed" : "Sign in to vote"}</button>`;
    return `<article class="project-card">${project.thumbnail_url ? `<img class="project-thumb" src="${esc(project.thumbnail_url)}" alt="" loading="lazy" />` : ""}<div><span class="badge" style="color:${project.track_color};border-color:${project.track_color}">${esc(project.track_name)}</span><h3>${esc(project.title)}</h3><p>${esc(project.tagline || project.summary)}</p><div class="tag-list">${(project.tech_tags || []).slice(0, 5).map((tag) => `<span class="badge">${esc(tag)}</span>`).join("")}</div></div><div class="card-footer"><span>${esc(project.team_name)}</span><span>${scoreLabel}</span></div><div class="card-actions"><button class="btn small ghost" data-project="${project.id}">View project</button>${voteControl}</div></article>`;
  }).join("") : `<div class="empty" style="grid-column:1/-1">No projects match that search.</div>`;
}
function bindGallery() {
  const apply = () => {
    const q = document.querySelector("#gallery-search").value.toLowerCase();
    const track = document.querySelector("#gallery-track").value;
    const tech = document.querySelector("#gallery-tech").value;
    const items = state.projects.filter((project) => (!q || `${project.title} ${project.summary} ${project.team_name} ${(project.tech_tags || []).join(" ")}`.toLowerCase().includes(q)) && (!track || project.track_name === track) && (!tech || (project.tech_tags || []).includes(tech)));
    document.querySelector("#gallery-grid").innerHTML = galleryCards(items);
  };
  document.querySelector("#gallery-search").addEventListener("input", apply);
  document.querySelector("#gallery-track").addEventListener("change", apply);
    document.querySelector("#gallery-tech").addEventListener("change", apply);
  document.querySelector("#gallery-grid").addEventListener("click", async (event) => {
     const projectButton = event.target.closest("[data-project]");
     if (projectButton) {
       try { await showGalleryProject(Number(projectButton.dataset.project)); } catch (error) { notify(error.message); }
       return;
     }
    const button = event.target.closest("[data-vote]");
    if (!button) return;
    try {
      const voterKey = localStorage.getItem("hacknight_voter_key") || crypto.randomUUID();
      localStorage.setItem("hacknight_voter_key", voterKey);
      const quantity = Number(document.querySelector(`[data-vote-quantity="${button.dataset.vote}"]`)?.value || 1);
      const email = button.dataset.emailVote ? window.prompt("Enter your email to vote")?.trim() : "";
      if (button.dataset.emailVote && (!email || !email.includes("@"))) { notify("A valid email is required for this ballot"); return; }
      await api("/api/votes", { method: "POST", headers: { "x-voter-key": voterKey, ...(email ? { "x-voter-email": email } : {}) }, body: JSON.stringify({ project_id: Number(button.dataset.vote), quantity }) });
       button.textContent = "Vote recorded";
      button.disabled = true;
    } catch (error) { notify(error.message); }
  });
}

async function showGalleryProject(projectId) {
  const detail = await api(`/api/projects/${projectId}`);
  const project = detail.project;
  const gallery = (project.image_urls || []).map((url) => `<img class="detail-image" src="${esc(url)}" alt="" loading="lazy" />`).join("");
  document.body.insertAdjacentHTML("beforeend", `<div class="modal-backdrop" id="gallery-modal"><section class="modal-card"><button class="modal-close" id="close-gallery-modal">Close</button><span class="badge" style="color:${project.track_color};border-color:${project.track_color}">${esc(project.track_name)}</span><h2>${esc(project.title)}</h2><p class="muted">${esc(project.team_name)} · ${detail.votes == null ? "Results hidden" : `${detail.votes} community votes`}</p>${project.thumbnail_url ? `<img class="detail-image" src="${esc(project.thumbnail_url)}" alt="${esc(project.title)}" />` : ""}${gallery}<p>${esc(project.long_description || project.summary)}</p><div class="tag-list">${(project.tech_tags || []).map((tag) => `<span class="badge">${esc(tag)}</span>`).join("")}</div><div class="modal-links"><a class="btn small" href="${esc(project.repo_url)}" target="_blank" rel="noreferrer">Repository</a><a class="btn small" href="${esc(project.demo_url)}" target="_blank" rel="noreferrer">Live demo</a>${project.demo_video_url ? `<a class="btn small" href="${esc(project.demo_video_url)}" target="_blank" rel="noreferrer">Demo video</a>` : ""}</div><div class="comment-list">${detail.comments.map((item) => `<div class="comment"><strong>${esc(item.author_name)}</strong><span>${esc(item.body)}</span><button class="btn small ghost" data-report-comment="${item.id}">Report</button></div>`).join("") || `<p class="muted">No comments yet.</p>`}</div>${detail.comments_enabled ? `<form id="comment-form" class="comment-form"><input class="input" name="body" placeholder="Leave a constructive comment" maxlength="500" required /><button class="btn small primary">Post</button></form>` : `<p class="notice">Comments are disabled for this event.</p>`}</section></div>`);
  document.querySelector("#close-gallery-modal").addEventListener("click", () => document.querySelector("#gallery-modal")?.remove());
  document.querySelector("#gallery-modal").addEventListener("click", (event) => { if (event.target.id === "gallery-modal") event.currentTarget.remove(); });
  document.querySelector("#comment-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = new FormData(event.currentTarget).get("body");
    try { await api("/api/comments", { method: "POST", body: JSON.stringify({ project_id: projectId, body }) }); document.querySelector("#gallery-modal")?.remove(); await showGalleryProject(projectId); notify("Comment posted"); } catch (error) { notify(error.message); }
  });
  document.querySelectorAll("[data-report-comment]").forEach((button) => button.addEventListener("click", async () => {
    const reason = window.prompt("Why should this comment be reviewed?");
    if (!reason) return;
    try { await api(`/api/comments/${button.dataset.reportComment}/report`, { method: "POST", body: JSON.stringify({ reason }) }); notify("Comment reported"); } catch (error) { notify(error.message); }
  }));
}

function resultsPage() {
  const data = state.publicResults || {};
  if (!data.published) {
    return `<div class="section-head"><div><h2>Results</h2><p>Final rankings are released by the organizer after judging is complete.</p></div><span class="badge yellow">PRIVATE</span></div><section class="panel empty">Results are not public yet. The event team is still reviewing submissions.</section>`;
  }
  return `<div class="section-head"><div><h2>Published results</h2><p>${esc(data.event?.name || "Hacknight")} · normalized scores are shown on a 100-point scale.</p></div><span class="badge cyan">PUBLISHED</span></div><section class="panel"><table class="table"><thead><tr><th>Rank</th><th>Project</th><th>Team</th><th>Score</th><th>Prize</th></tr></thead><tbody>${(data.items || []).map((item, index) => { const awards = (data.awards || []).filter((award) => award.project_id === item.project_id); return `<tr><td class="mono">${item.normalized_rank || index + 1}</td><td><span class="project-title">${esc(item.title)}</span></td><td>${esc(item.team_name)}</td><td><strong style="color:var(--pink)">${item.normalized_score ?? "—"}</strong> / 100</td><td>${awards.map((award) => `<span class="badge pink">${esc(award.title)}</span>`).join(" ") || `<span class="muted">—</span>`}</td></tr>`; }).join("") || `<tr><td colspan="5" class="muted">No ranked projects yet.</td></tr>`}</tbody></table></section>${data.feedback_visible ? `<div class="section-head"><div><h2>Released feedback</h2><p>Selected written feedback from judges is shared after the organizer releases it.</p></div></div><section class="panel">${(data.items || []).flatMap((item) => (item.feedback || []).map((feedback) => `<div class="comment"><strong>${esc(item.title)} · ${esc(feedback.criterion)}</strong><span>${esc(feedback.note)}</span></div>`)).join("") || `<p class="muted">No written feedback has been released yet.</p>`}</section>` : ""}`;
}

function submitPage() {
  const project = state.myProjects[0];
  const tracks = state.events[0]?.tracks || [];
  const questions = state.events[0]?.custom_questions || [];
  return `
    <div class="section-head"><div><h2>My submission</h2><p>Draft freely. The deadline is enforced when you publish.</p></div><span class="badge yellow">DUE ${formatDate(state.events[0]?.deadline)}</span></div>
    <div class="notice">Your project can be edited until the event deadline. Once submitted, judges see the published version assigned to them. Each team may submit one project.</div>
    <form class="panel form-grid" id="project-form">
      <div class="form-field"><label>Project title</label><input class="input" name="title" value="${esc(project?.title || "")}" required /></div>
      <div class="form-field"><label>Track</label><select class="select" name="track_id">${tracks.map((track) => `<option value="${track.id}" ${project?.track_id === track.id ? "selected" : ""}>${esc(track.name)}</option>`).join("")}</select></div>
      <div class="form-field full"><label>Tagline</label><input class="input" name="tagline" value="${esc(project?.tagline || "")}" placeholder="The one sentence people remember" /></div>
      <div class="form-field full"><label>One-line summary</label><textarea class="textarea" name="summary" required>${esc(project?.summary || "")}</textarea></div>
      <div class="form-field full"><label>Long description</label><textarea class="textarea" name="long_description" placeholder="What did you build, why, and how does it work?">${esc(project?.long_description || "")}</textarea></div>
      <div class="form-field"><label>Thumbnail URL</label><input class="input" name="thumbnail_url" type="url" value="${esc(project?.thumbnail_url || "")}" placeholder="https://…" /></div>
      <div class="form-field"><label>Image gallery URLs</label><input class="input" name="image_urls" value="${esc((project?.image_urls || []).join(", "))}" placeholder="https://… , https://…" /></div>
      <div class="form-field"><label>Demo video URL</label><input class="input" name="demo_video_url" type="url" value="${esc(project?.demo_video_url || "")}" placeholder="https://…" /></div>
      <div class="form-field"><label>Technology tags</label><input class="input" name="tech_tags" value="${esc((project?.tech_tags || []).join(", "))}" placeholder="FastAPI, SQLite, offline-first" /></div>
      <div class="form-field"><label>Repository URL</label><input class="input" name="repo_url" value="${esc(project?.repo_url || "https://github.com/")}" required /></div>
      <div class="form-field"><label>Demo URL</label><input class="input" name="demo_url" value="${esc(project?.demo_url || "https://")}" required /></div>
      ${questions.map((question) => `<div class="form-field ${question.question_type === "textarea" ? "full" : ""}"><label>${esc(question.prompt)}${question.required ? " *" : ""}</label>${question.question_type === "textarea" ? `<textarea class="textarea" name="custom_${esc(question.question_key)}" ${question.required ? "required" : ""}>${esc(project?.custom_answers?.[question.question_key] || "")}</textarea>` : `<input class="input" name="custom_${esc(question.question_key)}" value="${esc(project?.custom_answers?.[question.question_key] || "")}" ${question.required ? "required" : ""} />`}</div>`).join("")}
      <div class="form-field"><label>Submission state</label><select class="select" name="status"><option value="draft" ${project?.status !== "submitted" ? "selected" : ""}>Save as draft</option><option value="submitted" ${project?.status === "submitted" ? "selected" : ""}>Publish submission</option></select></div>
      <div class="form-actions"><button class="btn ghost" type="button" data-page="overview">Cancel</button><button class="btn primary">${project ? "Save changes" : "Create project"}</button></div>
    </form>
    <div class="section-head"><div><h2>Team room</h2><p>Invite teammates with a local link code. Membership is checked by the API.</p></div></div>
    <section class="panel">
       ${state.team?.team ? `<div class="grid two-col"><div><div class="eyebrow">YOUR TEAM</div><h2 style="margin:10px 0 4px">${esc(state.team.team.name)}</h2><p class="muted" style="font-size:12px">${state.team.members.length} member${state.team.members.length === 1 ? "" : "s"} · invite code <span class="mono" style="color:var(--cyan)">${esc(state.team.team.invite_code)}</span></p><p class="muted" style="font-size:11px">${state.team.members.map((member) => `${esc(member.name)} · ${esc(member.member_role)}`).join("<br>")}</p><button class="btn small ghost" id="leave-team">Leave team</button></div><form id="join-team-form" class="form-field"><label>Join another team</label><div style="display:flex;gap:8px"><input class="input" name="invite_code" placeholder="Paste invite code" required /><button class="btn small">Join</button></div></form></div>` : `<form id="create-team-form" class="form-grid"><div class="form-field"><label>Create your team</label><input class="input" name="name" placeholder="Team name" required /></div><div class="form-actions"><button class="btn primary">Create team</button></div></form>`}
    </section>`;
}
async function bindSubmit() {
  const form = document.querySelector("#project-form");
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); const data = new FormData(form);
    const existingProject = state.myProjects[0];
    if (!state.team?.team) { notify("Create or join a team before submitting"); return; }
    const custom_answers = {};
    [...form.querySelectorAll("[name^='custom_']")].forEach((field) => { custom_answers[field.name.slice(7)] = field.value; });
    const payload = {
      event_id: state.events[0].id, team_id: state.team.team.id, track_id: Number(data.get("track_id")),
      title: data.get("title"), summary: data.get("summary"), tagline: data.get("tagline"),
      long_description: data.get("long_description"), thumbnail_url: data.get("thumbnail_url"),
      image_urls: String(data.get("image_urls") || "").split(",").map((item) => item.trim()).filter(Boolean),
      demo_video_url: data.get("demo_video_url"), tech_tags: String(data.get("tech_tags") || "").split(",").map((item) => item.trim()).filter(Boolean),
      custom_answers, repo_url: data.get("repo_url"), demo_url: data.get("demo_url"), status: data.get("status")
    };
    try { await api(existingProject ? `/api/projects/${existingProject.id}` : "/api/projects", { method: existingProject ? "PUT" : "POST", body: JSON.stringify(payload) }); notify("Submission saved"); await loadPage("submit"); } catch (error) { notify(error.message); }
  });
  const joinForm = document.querySelector("#join-team-form");
  if (joinForm) joinForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const code = new FormData(joinForm).get("invite_code");
    try { await api("/api/teams/join", { method: "POST", body: JSON.stringify({ invite_code: code }) }); notify("You joined the team"); await loadPage("submit"); } catch (error) { notify(error.message); }
  });
  const createTeamForm = document.querySelector("#create-team-form");
  if (createTeamForm) createTeamForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = new FormData(createTeamForm).get("name");
    try { await api("/api/teams", { method: "POST", body: JSON.stringify({ event_id: state.events[0].id, name }) }); notify("Team created"); await loadPage("submit"); } catch (error) { notify(error.message); }
  });
  document.querySelector("#leave-team")?.addEventListener("click", async () => {
    if (!window.confirm("Leave this team?")) return;
    try { await api(`/api/teams/${state.team.team.id}/members/me`, { method: "DELETE" }); notify("You left the team"); await loadPage("submit"); } catch (error) { notify(error.message); }
  });
}

function judgePage() {
  return `
     <div class="section-head"><div><h2>Your judge queue</h2><p>Only assigned projects are visible. Other judges' scores never appear here.</p></div><div class="top-actions"><span class="badge pink">${state.assignments.length} ASSIGNED</span><span class="badge cyan">${state.judgeSummary?.completed || 0} COMPLETE · ${state.judgeSummary?.remaining || 0} REMAINING</span></div></div>
    <section class="panel"><table class="table"><thead><tr><th>Project</th><th>Track</th><th>Progress</th><th>Action</th></tr></thead><tbody>${state.assignments.map((item) => `<tr><td><span class="project-title">${esc(item.title)}</span><span class="project-sub">${esc(item.team_name)}</span></td><td><span class="badge" style="color:${item.track_color};border-color:${item.track_color}">${esc(item.track_name)}</span></td><td class="mono">${item.scored_criteria} / ${item.criteria_count} criteria</td><td><button class="btn small primary" data-judge-project="${item.id}">${item.scored_criteria ? "Continue scoring" : "Start scoring"}</button></td></tr>`).join("")}</tbody></table></section>
    <div class="section-head"><div><h2>Pairwise calibration</h2><p>Optional head-to-head comparisons provide a second, transparent signal alongside the weighted rubric.</p></div><span class="badge cyan">${state.pairwise?.completed || 0} COMPLETE</span></div>
    <section class="panel">${state.pairwise?.pair ? `<div class="pairwise-grid"><article class="pair-card"><span class="eyebrow">PROJECT A</span><h3>${esc(state.pairwise.pair.left.title)}</h3><p>${esc(state.pairwise.pair.left.summary)}</p><button class="btn primary" data-pairwise-winner="${state.pairwise.pair.left.id}">Prefer this project</button></article><div class="pairwise-or">VS</div><article class="pair-card"><span class="eyebrow">PROJECT B</span><h3>${esc(state.pairwise.pair.right.title)}</h3><p>${esc(state.pairwise.pair.right.summary)}</p><button class="btn primary" data-pairwise-winner="${state.pairwise.pair.right.id}">Prefer this project</button></article></div><p class="muted" style="margin-top:14px">${state.pairwise.remaining} comparison${state.pairwise.remaining === 1 ? "" : "s"} remaining in your assigned set.</p>` : `<div class="empty">No unreviewed project pair is available. Pairwise comparisons will appear when you have two assigned projects from different teams.</div>`}</section>
    <div id="judge-detail"></div>`;
}
async function bindJudge() {
  document.querySelectorAll("[data-judge-project]").forEach((button) => button.addEventListener("click", async () => {
    try { state.activeProject = await api(`/api/judge/projects/${button.dataset.judgeProject}`); renderJudgeDetail(); } catch (error) { notify(error.message); }
  }));
  document.querySelectorAll("[data-pairwise-winner]").forEach((button) => button.addEventListener("click", async () => {
    const pair = state.pairwise?.pair;
    if (!pair) return;
    try {
      await api("/api/judge/pairwise", { method: "POST", body: JSON.stringify({ event_id: state.events[0].id, project_a_id: pair.left.id, project_b_id: pair.right.id, winner_project_id: Number(button.dataset.pairwiseWinner) }) });
      notify("Pairwise comparison saved");
      await loadPage("judge");
    } catch (error) { notify(error.message); }
  }));
}
function renderJudgeDetail() {
  const detail = document.querySelector("#judge-detail"); if (!detail || !state.activeProject) return;
  const { project, criteria, scores } = state.activeProject; const scoreMap = Object.fromEntries(scores.map((score) => [score.criterion_id, score.score]));
  detail.innerHTML = `<div class="section-head"><div><h2>Score ${esc(project.title)}</h2><p>${esc(project.summary)}</p></div><a class="btn small" href="${esc(project.demo_url)}" target="_blank" rel="noreferrer">Open demo</a></div><section class="panel"><form id="score-form">${criteria.map((criterion) => `<div class="criterion"><div><div class="criterion-name">${esc(criterion.name)} <span class="badge pink">${criterion.weight}%</span></div><div class="criterion-description">${esc(criterion.description)}</div></div><div class="score-control">${[1,2,3,4,5].map((score) => `<button type="button" class="${scoreMap[criterion.id] === score ? "selected" : ""}" data-criterion="${criterion.id}" data-score="${score}">${score}</button>`).join("")}</div></div>`).join("")}<div class="form-field" style="margin-top:16px"><label>Private judge note</label><textarea class="textarea" name="note" placeholder="What should the organizer know?"></textarea></div><div class="form-actions"><button class="btn primary">Save scorecard</button></div></form></section>`;
  const selected = { ...scoreMap };
  detail.querySelectorAll("[data-criterion]").forEach((button) => button.addEventListener("click", () => { selected[button.dataset.criterion] = Number(button.dataset.score); detail.querySelectorAll(`[data-criterion="${button.dataset.criterion}"]`).forEach((item) => item.classList.toggle("selected", item === button)); }));
  detail.querySelector("#score-form").addEventListener("submit", async (event) => { event.preventDefault(); const note = new FormData(event.currentTarget).get("note"); try { await api(`/api/judge/projects/${project.id}/scores`, { method: "POST", body: JSON.stringify({ scores: selected, note }) }); notify("Scorecard saved privately"); state.activeProject = null; await loadPage("judge"); } catch (error) { notify(error.message); } });
}

function normalizationProof() {
  const proof = state.normalization || {};
  const profiles = proof.judge_profiles || [];
  const changes = proof.ranking_changes || [];
  const scoreRows = profiles.flatMap((profile) => profile.scores || []);
  return `
    <div class="section-head"><div><h2>Normalization proof</h2><p>Raw judge scores are transformed transparently before the final ranking is published.</p></div><span class="badge cyan">RAW → NORMALIZED → RANKED</span></div>
    <section class="panel">
      <div class="eyebrow">METHOD</div>
      <h3 style="margin:10px 0 8px">${esc(proof.method || "Per-judge z-score standardization")}</h3>
      <p class="muted" style="line-height:1.6">Each judge is calibrated against their own score distribution. This prevents a consistently strict or generous judge from dominating the result. The weighted rubric is applied after calibration.</p>
      <div class="mono" style="margin-top:16px;padding:14px;border:1px solid var(--line);color:var(--cyan);overflow:auto">${esc(proof.formula || "normalized = clamp(50 + 10 × ((raw - judge_mean) / judge_stddev), 0, 100)")}</div>
      <div class="grid three-col" style="margin-top:16px">
        <div class="metric"><div class="label">Center</div><div class="value">${proof.midpoint ?? 50}</div><div class="trend">normalized midpoint</div></div>
        <div class="metric"><div class="label">Spread</div><div class="value">${proof.spread ?? 10}</div><div class="trend">standard deviations ×</div></div>
        <div class="metric"><div class="label">Observed scores</div><div class="value">${scoreRows.length}</div><div class="trend">criterion scores transformed</div></div>
      </div>
    </section>
    <div class="section-head"><div><h2>Judge calibration</h2><p>These values are the evidence behind each raw-to-normalized conversion.</p></div></div>
    <section class="panel"><table class="table"><thead><tr><th>Judge</th><th>Scores</th><th>Mean</th><th>Std. dev.</th><th>Transformation samples</th></tr></thead><tbody>${profiles.length ? profiles.map((profile) => `<tr><td><span class="project-title">${esc(profile.judge_name)}</span></td><td class="mono">${profile.score_count}</td><td class="mono">${profile.mean}</td><td class="mono">${profile.stddev}</td><td class="mono">${(profile.scores || []).slice(0, 3).map((score) => `${score.raw_score} → ${score.normalized_score}`).join(" · ")}</td></tr>`).join("") : `<tr><td colspan="5" class="muted">Submit scorecards to generate judge calibration evidence.</td></tr>`}</tbody></table></section>
    <div class="section-head"><div><h2>Ranking movement</h2><p>The same projects are shown before and after normalization so the final ordering can be audited.</p></div></div>
    <section class="panel"><table class="table"><thead><tr><th>Final rank</th><th>Project</th><th>Raw rank</th><th>Raw weighted</th><th>Normalized</th><th>Movement</th></tr></thead><tbody>${changes.length ? changes.map((item) => {
      const movement = item.rank_delta > 0 ? `↑ ${item.rank_delta}` : item.rank_delta < 0 ? `↓ ${Math.abs(item.rank_delta)}` : "—";
      return `<tr><td class="mono">${item.normalized_rank}</td><td><span class="project-title">${esc(item.title)}</span></td><td class="mono">${item.raw_rank}</td><td class="mono">${item.raw_score}</td><td><strong style="color:var(--pink)">${item.normalized_score}</strong> / 100</td><td class="mono">${movement}</td></tr>`;
    }).join("") : `<tr><td colspan="6" class="muted">The ranking proof appears after at least one judge submits a scorecard.</td></tr>`}</tbody></table></section>
    ${profiles.length ? `<details class="panel"><summary style="cursor:pointer;color:var(--cyan)">Show every raw-to-normalized score contribution</summary><div style="overflow:auto;margin-top:16px"><table class="table"><thead><tr><th>Judge</th><th>Project</th><th>Criterion</th><th>Weight</th><th>Raw</th><th>Normalized</th><th>Weighted raw</th><th>Weighted normalized</th></tr></thead><tbody>${profiles.flatMap((profile) => (profile.scores || []).map((score) => `<tr><td>${esc(profile.judge_name)}</td><td>${esc(score.project_title)}</td><td>${esc(score.criterion_name)}</td><td class="mono">${score.weight}%</td><td class="mono">${score.raw_score}</td><td class="mono">${score.normalized_score}</td><td class="mono">${score.raw_contribution}</td><td class="mono">${score.normalized_contribution}</td></tr>`)).join("")}</tbody></table></div></details>` : ""}`;
}

function pairwiseProof() {
  const data = state.pairwiseOrganizer || {};
  const items = data.items || [];
  return `<div class="section-head"><div><h2>Pairwise ranking</h2><p>Head-to-head comparisons are fitted with a Bradley–Terry model and remain separate from the official weighted score.</p></div><span class="badge cyan">${data.comparison_count || 0} COMPARISONS</span></div><section class="panel"><table class="table"><thead><tr><th>Rank</th><th>Project</th><th>Team</th><th>Comparisons</th><th>Wins</th><th>BT strength</th></tr></thead><tbody>${items.map((item) => `<tr><td class="mono">${item.bt_rank}</td><td><span class="project-title">${esc(item.title)}</span></td><td>${esc(item.team_name)}</td><td class="mono">${item.comparisons}</td><td class="mono">${item.wins}</td><td class="mono">${item.bt_score}</td></tr>`).join("") || `<tr><td colspan="6" class="muted">Pairwise results appear after assigned judges submit comparisons.</td></tr>`}</tbody></table></section>`;
}

function organizerPage() {
  const counts = state.overview?.counts || {};
  const event = state.events[0] || {};
  return `<div class="section-head"><div><h2>Organizer desk</h2><p>Configure the event, watch judging progress, and export the record.</p></div><div class="top-actions"><span class="badge ${event.results_published ? "cyan" : "yellow"}">${event.results_published ? "RESULTS PUBLISHED" : `PHASE ${String(event.phase || "draft").replaceAll("_", " ").toUpperCase()}`}</span><button class="btn small" id="toggle-results">${event.results_published ? "Unpublish results" : "Publish results"}</button><a class="btn small" href="/api/organizer/export/projects.csv">Projects CSV</a><a class="btn small" href="/api/organizer/export/assignments.csv">Assignments CSV</a><a class="btn small" href="/api/organizer/export/normalized.csv">Normalized CSV</a><a class="btn small" href="/api/organizer/export/results.csv">Results CSV</a><a class="btn small" href="/api/organizer/export/event.json">Event JSON</a><a class="btn primary" href="/api/organizer/export/scores.csv">Scores CSV</a></div></div>
    <div class="grid metrics"><div class="metric"><div class="label">Submitted</div><div class="value">${counts.submitted || 0}</div><div class="trend">publicly visible</div></div><div class="metric"><div class="label">Assignments</div><div class="value">${counts.assignments || 0}</div><div class="trend">across ${counts.judges || 0} judges</div></div><div class="metric"><div class="label">Scorecards</div><div class="value">${counts.scores || 0}</div><div class="trend">private until publish</div></div><div class="metric"><div class="label">Rubric</div><div class="value">${counts.criteria || 0}</div><div class="trend">weighted criteria</div></div></div>
     <div class="section-head"><div><h2>Weighted rubric</h2><p>All judges score the same defensible rubric, with weights totaling 100%.</p></div></div>
     <form class="panel" id="rubric-form"><div class="rubric-note">Weights must total 100%. Changes apply to future and existing scorecards without exposing private judge notes.</div>${(state.overview?.criteria || []).map((criterion) => `<div class="rubric-row" data-criterion-id="${criterion.id}"><div class="form-field"><label>Criterion</label><input class="input" data-rubric-name value="${esc(criterion.name)}" required /></div><div class="form-field"><label>Weight</label><input class="input" data-rubric-weight type="number" min="1" max="100" value="${criterion.weight}" required /></div><div class="form-field full"><label>What judges look for</label><textarea class="textarea compact" data-rubric-description required>${esc(criterion.description)}</textarea></div></div>`).join("")}<div class="form-actions"><button class="btn primary">Save rubric</button></div></form>
     <div class="section-head"><div><h2>Active ballot policy</h2><p>Access, voting window, result release, and anti-position-bias settings apply at the API boundary.</p></div></div>
      <form class="panel form-grid" id="voting-policy-form"><div class="form-field"><label>Voting access</label><select class="select" name="voting_access"><option value="open" ${event.voting_access === "open" ? "selected" : ""}>Open link</option><option value="email" ${event.voting_access === "email" ? "selected" : ""}>Email-gated</option><option value="authenticated" ${event.voting_access === "authenticated" ? "selected" : ""}>Authenticated</option></select></div><div class="form-field"><label>Voting mode</label><select class="select" name="voting_mode"><option value="one_per_project" ${event.voting_mode === "one_per_project" ? "selected" : ""}>One vote per project</option><option value="one_per_event" ${event.voting_mode === "one_per_event" ? "selected" : ""}>One vote per event</option><option value="quadratic" ${event.voting_mode === "quadratic" ? "selected" : ""}>Quadratic voting</option></select></div><div class="form-field"><label>Voting opens</label><input class="input" name="voting_opens_at" type="datetime-local" value="${inputDate(event.voting_opens_at)}" /></div><div class="form-field"><label>Voting closes</label><input class="input" name="voting_closes_at" type="datetime-local" value="${inputDate(event.voting_closes_at)}" /></div><div class="form-field"><label>Quadratic budget</label><input class="input" name="quadratic_budget" type="number" min="1" value="${event.quadratic_budget || 16}" /></div><label class="check-field"><input type="checkbox" name="results_visible" ${event.results_visible ? "checked" : ""} /> Publish community vote counts</label><div class="form-actions"><button class="btn primary">Save ballot policy</button></div></form>
      <form class="panel form-grid" id="feedback-policy-form"><div><div class="eyebrow">FEEDBACK RELEASE</div><p class="muted">Release written judge feedback separately from scores.</p></div><label class="check-field"><input type="checkbox" name="feedback_visible" ${event.feedback_visible ? "checked" : ""} /> Show selected feedback on the public results page</label><div class="form-actions"><button class="btn primary">Save feedback release</button></div></form>
    <div class="section-head"><div><h2>Normalized ranking</h2><p>Raw judge scores are standardized per judge before weighted aggregation.</p></div><span class="badge cyan">ORGANIZER ONLY</span></div>
     <section class="panel"><table class="table"><thead><tr><th>Rank</th><th>Project</th><th>Judges</th><th>Raw weighted</th><th>Normalized</th></tr></thead><tbody>${state.results.length ? state.results.map((item, index) => `<tr><td class="mono">${item.normalized_rank || index + 1}</td><td><span class="project-title">${esc(item.title)}</span><span class="project-sub">${esc(item.team_name)}</span></td><td class="mono">${item.judges.join(", ")}</td><td class="mono">${item.raw_score ?? "—"}</td><td><strong style="color:var(--pink)">${item.normalized_score ?? "—"}</strong> / 100</td></tr>`).join("") : `<tr><td colspan="5" class="muted">Scores appear here after judges submit scorecards.</td></tr>`}</tbody></table></section>
       ${normalizationProof()}
       ${pairwiseProof()}
      <div class="section-head"><div><h2>Prize awards</h2><p>Select prize winners and publish them with the final results.</p></div></div>
      <form class="panel form-grid" id="award-form"><div class="form-field"><label>Prize or special award</label><select class="select" name="prize_id"><option value="">Special award</option>${(event.prizes || []).map((prize) => `<option value="${prize.id}">${esc(prize.title)} · ${esc(prize.amount)}</option>`).join("")}</select></div><div class="form-field"><label>Project</label><select class="select" name="project_id"><option value="">No project selected</option>${state.projects.filter((item) => item.status === "submitted").map((item) => `<option value="${item.id}">${esc(item.title)} · ${esc(item.team_name)}</option>`).join("")}</select></div><div class="form-field"><label>Award title</label><input class="input" name="title" value="Best in show" required /></div><div class="form-field"><label>Recipient</label><input class="input" name="recipient_name" placeholder="Team or participant" /></div><div class="form-actions"><button class="btn primary">Save award</button></div></form>
      <section class="panel" style="margin-top:16px"><table class="table"><thead><tr><th>Award</th><th>Project</th><th>Recipient</th></tr></thead><tbody>${state.awards.map((award) => `<tr><td>${esc(award.title)}</td><td>${esc(award.project_title || "—")}</td><td>${esc(award.recipient_name || "—")}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">No awards selected.</td></tr>`}</tbody></table></section>
      <div class="section-head"><div><h2>Comment moderation</h2><p>Reported comments are listed first. Organizers can hide, restore, or remove community content.</p></div></div>
      <section class="panel"><table class="table"><thead><tr><th>Project</th><th>Author</th><th>Comment</th><th>Status</th><th>Action</th></tr></thead><tbody>${state.moderationComments.map((item) => `<tr><td>${esc(item.project_title)}</td><td>${esc(item.author_name)}</td><td>${esc(item.body)}</td><td><span class="badge ${item.status === "reported" ? "yellow" : ""}">${esc(item.status)}</span></td><td><select class="select moderation-status" data-comment-id="${item.id}"><option value="active" ${item.status === "active" ? "selected" : ""}>Active</option><option value="hidden" ${item.status === "hidden" ? "selected" : ""}>Hidden</option><option value="removed" ${item.status === "removed" ? "selected" : ""}>Removed</option></select></td></tr>`).join("") || `<tr><td colspan="5" class="muted">No comments to moderate.</td></tr>`}</tbody></table></section>
      <div class="section-head"><div><h2>Judge invitations</h2><p>Invite a judge by email, then assign submitted projects from this desk.</p></div></div>
    <form class="panel form-grid" id="invite-form"><div class="form-field"><label>Judge email</label><input class="input" name="email" type="email" placeholder="judge@example.local" required /></div><div class="form-actions"><button class="btn primary">Create invitation</button></div></form>
    <section class="panel" style="margin-top:16px"><table class="table"><thead><tr><th>Email</th><th>Status</th><th>Created</th></tr></thead><tbody>${state.invitations.map((item) => `<tr><td>${esc(item.email)}</td><td><span class="badge yellow">${esc(item.status)}</span></td><td class="mono">${formatDate(item.created_at)}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">No invitations yet.</td></tr>`}</tbody></table></section>
       <div class="section-head"><div><h2>Project assignments</h2><p>Batch-select projects for a judge. Team conflicts and duplicate assignments are rejected by the API.</p></div></div>
      <form class="panel form-grid" id="assignment-form"><div class="form-field"><label>Judge</label><select class="select" name="judge_id" required>${state.judges.map((judge) => `<option value="${judge.id}">${esc(judge.name)} · ${esc(judge.email)}</option>`).join("")}</select></div><div class="form-field"><label>Projects</label><div class="assignment-list">${state.projects.filter((item) => item.status === "submitted").map((project) => { const assigned = state.assignments.filter((item) => item.id === project.id).map((item) => item.judge_name); return `<label class="check-field"><input type="checkbox" name="project_ids" value="${project.id}" /> <span>${esc(project.title)} <small class="muted">· ${esc(project.team_name)}${assigned.length ? ` · assigned to ${esc(assigned.join(", "))}` : ""}</small></span></label>`; }).join("") || `<p class="muted">No submitted projects available.</p>`}</div></div><div class="form-actions"><button class="btn primary">Assign selected projects</button></div></form>
       <form class="panel form-grid" id="auto-assignment-form"><div class="form-field"><label>Automatic review target</label><input class="input" name="reviews_per_project" type="number" min="1" max="10" value="2" /><small class="muted">Distributes projects evenly while respecting track permissions and team conflicts.</small></div><div class="form-actions"><button class="btn">Run automatic assignment</button></div></form>
      <div class="section-head"><div><h2>Audit trail</h2><p>Searchable record of access decisions, submissions, judging, votes, and exports.</p></div><a class="btn small" href="/api/organizer/export/audit.csv">Audit CSV</a></div>
      <section class="panel"><div class="filters"><input id="audit-search" class="input" placeholder="Search user, action, resource, or ID" /><select id="audit-entity" class="select" style="max-width:220px"><option value="">All resources</option>${[...new Set(state.auditLogs.map((item) => item.entity))].sort().map((entity) => `<option>${esc(entity)}</option>`).join("")}</select></div><div style="overflow:auto"><table class="table"><thead><tr><th>When</th><th>User</th><th>Action</th><th>Resource</th><th>Details</th></tr></thead><tbody id="audit-log-rows">${state.auditLogs.map((item) => `<tr><td class="mono">${formatDate(item.created_at)}</td><td>${esc(item.user_name)}</td><td><span class="badge">${esc(item.action)}</span></td><td>${esc(item.entity)} <span class="project-sub">#${esc(item.entity_id)}</span></td><td class="mono">${esc(JSON.stringify(item.metadata || {}))}</td></tr>`).join("") || `<tr><td colspan="5" class="muted">No audit events yet.</td></tr>`}</tbody></table></div></section>
     <div class="section-head"><div><h2>Bulk project import</h2><p>Paste a JSON array to import projects and create missing teams or tracks automatically.</p></div></div>
     <form class="panel form-grid" id="import-form"><div class="form-field full"><label>Project rows (JSON)</label><textarea class="textarea" name="rows" placeholder='[{"team_name":"Team Atlas","track_name":"General","title":"Project","summary":"A useful project summary","repo_url":"","demo_url":"","status":"submitted"}]'>[]</textarea></div><div class="form-actions"><button class="btn primary">Import projects</button></div></form>
     <div class="section-head"><div><h2>Webhooks</h2><p>Receive signed event notifications without making them a runtime dependency.</p></div></div>
     <form class="panel form-grid" id="webhook-form"><div class="form-field"><label>Endpoint URL</label><input class="input" name="url" type="url" placeholder="http://localhost:9000/hacknight" required /></div><div class="form-field"><label>Events</label><input class="input" name="events" value="project.created,scorecard.submitted,vote.created" /></div><div class="form-actions"><button class="btn primary">Register webhook</button></div></form>
     <section class="panel" style="margin-top:16px"><table class="table"><thead><tr><th>Endpoint</th><th>Events</th><th>Deliveries</th></tr></thead><tbody>${state.webhooks.map((item) => `<tr><td>${esc(item.url)}</td><td class="mono">${esc(item.events)}</td><td class="mono">${item.delivery_count}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">No webhooks registered.</td></tr>`}</tbody></table></section>
     <div class="section-head"><div><h2>Certificates</h2><p>Issue a printable local certificate for a submitted project.</p></div></div>
     <form class="panel form-grid" id="certificate-form"><div class="form-field"><label>Project</label><select class="select" name="project_id">${state.projects.map((item) => `<option value="${item.id}">${esc(item.title)} · ${esc(item.team_name)}</option>`).join("")}</select></div><div class="form-field"><label>Recipient</label><input class="input" name="recipient_name" placeholder="Team or participant name" required /></div><div class="form-field"><label>Award</label><input class="input" name="award_title" value="Official Hacknight Selection" required /></div><div class="form-actions"><button class="btn primary">Issue certificate</button></div></form>
     <section class="panel" style="margin-top:16px"><table class="table"><thead><tr><th>Award</th><th>Recipient</th><th>Certificate</th></tr></thead><tbody>${state.certificates.map((item) => `<tr><td>${esc(item.award_title)}<span class="project-sub">${esc(item.project_title)}</span></td><td>${esc(item.recipient_name)}</td><td><a class="btn small" href="/api/certificates/${encodeURIComponent(item.certificate_code)}" target="_blank">Open</a></td></tr>`).join("") || `<tr><td colspan="3" class="muted">No certificates issued.</td></tr>`}</tbody></table></section>
     <div class="section-head"><div><h2>Judge participation records</h2><p>Issue a signed record that anyone can verify without organizer access.</p></div></div>
     <form class="panel form-grid" id="judge-record-form"><div class="form-field"><label>Judge</label><select class="select" name="judge_id">${state.judges.map((judge) => `<option value="${judge.id}">${esc(judge.name)} · ${esc(judge.email)}</option>`).join("")}</select></div><div class="form-actions"><button class="btn primary">Issue signed record</button></div></form>
     <section class="panel" style="margin-top:16px"><table class="table"><thead><tr><th>Judge</th><th>Issued</th><th>Verification</th></tr></thead><tbody>${state.judgeRecords.map((item) => `<tr><td>${esc(item.judge_name)}<span class="project-sub">${esc(item.event_name)}</span></td><td class="mono">${formatDate(item.created_at)}</td><td><a class="btn small" href="/api/judge-records/${encodeURIComponent(item.record_code)}" target="_blank">Verify JSON</a></td></tr>`).join("") || `<tr><td colspan="3" class="muted">No judge records issued.</td></tr>`}</tbody></table></section>
      ${state.user.role === "admin" ? `<div class="section-head"><div><h2>User access</h2><p>Admins can manage organizer, judge, participant, and admin roles.</p></div></div><section class="panel"><table class="table"><thead><tr><th>Name</th><th>Email</th><th>Current role</th><th>Change role</th></tr></thead><tbody>${state.adminUsers.map((item) => `<tr><td>${esc(item.name)}</td><td class="mono">${esc(item.email)}</td><td><span class="badge">${esc(item.role)}</span></td><td><select class="select admin-role" data-user-id="${item.id}"><option value="participant" ${item.role === "participant" ? "selected" : ""}>Participant</option><option value="judge" ${item.role === "judge" ? "selected" : ""}>Judge</option><option value="organizer" ${item.role === "organizer" ? "selected" : ""}>Organizer</option><option value="admin" ${item.role === "admin" ? "selected" : ""}>Admin</option></select></td></tr>`).join("")}</tbody></table></section>` : ""}
      <div class="section-head"><div><h2>Event creation</h2><p>Start another local event with its schedule, tracks, prizes, and voting policy.</p></div></div>
       <form class="panel form-grid" id="event-form"><div class="form-field"><label>Event name</label><input class="input" name="name" value="Hacknight One" required /></div><div class="form-field"><label>Tagline</label><input class="input" name="tagline" value="Make the next one better." required /></div><div class="form-field full"><label>Description</label><textarea class="textarea" name="description" required>Build useful infrastructure with a community of peers.</textarea></div><div class="form-field"><label>Hackathon starts</label><input class="input" name="starts_at" type="datetime-local" required /></div><div class="form-field"><label>Submission deadline</label><input class="input" name="deadline" type="datetime-local" required /></div><div class="form-field"><label>Registration opens</label><input class="input" name="registration_opens_at" type="datetime-local" /></div><div class="form-field"><label>Registration closes</label><input class="input" name="registration_closes_at" type="datetime-local" /></div><div class="form-field"><label>Hackathon ends</label><input class="input" name="hackathon_ends_at" type="datetime-local" /></div><div class="form-field"><label>Judging opens</label><input class="input" name="judging_opens_at" type="datetime-local" /></div><div class="form-field"><label>Judging closes</label><input class="input" name="judging_closes_at" type="datetime-local" /></div><div class="form-field"><label>Results publish date</label><input class="input" name="results_publish_at" type="datetime-local" /></div><div class="form-field"><label>Team minimum</label><input class="input" name="team_min_size" type="number" min="1" max="4" value="1" /></div><div class="form-field"><label>Team maximum</label><input class="input" name="team_max_size" type="number" min="1" max="4" value="4" /></div><label class="check-field"><input type="checkbox" name="comments_enabled" checked /> Enable public comments</label><div class="form-field full"><label>Tracks · one per line: name | color</label><textarea class="textarea compact" name="tracks">Open infrastructure | #ff3b86
Human systems | #25e0c2
Wildcard | #a78bfa</textarea></div><div class="form-field full"><label>Prizes · one per line: title | amount</label><textarea class="textarea compact" name="prizes">Best in show | $2,500
Most useful | $1,000</textarea></div><div class="form-field"><label>Voting access</label><select class="select" name="voting_access"><option value="open">Open link</option><option value="email">Email-gated</option><option value="authenticated" selected>Authenticated</option></select></div><div class="form-field"><label>Voting mode</label><select class="select" name="voting_mode"><option value="one_per_project">One per project</option><option value="one_per_event">One per event</option><option value="quadratic">Quadratic voting</option></select></div><div class="form-field"><label>Voting opens</label><input class="input" name="voting_opens_at" type="datetime-local" /></div><div class="form-field"><label>Voting closes</label><input class="input" name="voting_closes_at" type="datetime-local" /></div><div class="form-field"><label>Quadratic budget</label><input class="input" name="quadratic_budget" type="number" min="1" value="16" /></div><label class="check-field"><input type="checkbox" name="results_visible" /> Publish results during voting</label><div class="form-actions"><button class="btn primary">Create event</button></div></form>`;
}
function bindOrganizer() {
  const form = document.querySelector("#event-form"); if (!form) return;
  document.querySelector("#toggle-results")?.addEventListener("click", async () => {
    try {
      await api(`/api/organizer/results/${state.events[0].results_published ? "unpublish" : "publish"}`, {
        method: "POST",
        body: JSON.stringify({ event_id: state.events[0].id }),
      });
      notify(state.events[0].results_published ? "Results unpublished" : "Results published");
      await loadPage("organizer");
    } catch (error) { notify(error.message); }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    try {
      await api("/api/events", {
        method: "POST",
        body: JSON.stringify({
           name: data.get("name"), tagline: data.get("tagline"), description: data.get("description"),
           starts_at: data.get("starts_at"), deadline: data.get("deadline"),
           registration_opens_at: data.get("registration_opens_at") || null,
           registration_closes_at: data.get("registration_closes_at") || null,
           hackathon_ends_at: data.get("hackathon_ends_at") || null,
           judging_opens_at: data.get("judging_opens_at") || null,
           judging_closes_at: data.get("judging_closes_at") || null,
           results_publish_at: data.get("results_publish_at") || null,
           team_min_size: Number(data.get("team_min_size") || 1),
           team_max_size: Number(data.get("team_max_size") || 4),
           comments_enabled: data.get("comments_enabled") === "on",
           tracks: String(data.get("tracks")).split("\n").map((line) => line.split("|")).filter((parts) => parts[0]?.trim()).map((parts, index) => ({ name: parts[0].trim(), color: parts[1]?.trim() || ["#ff3b86", "#25e0c2", "#a78bfa"][index % 3] })),
           prizes: String(data.get("prizes")).split("\n").map((line) => line.split("|")).filter((parts) => parts[0]?.trim()).map((parts) => ({ title: parts[0].trim(), amount: parts[1]?.trim() || "" })),
          voting_access: data.get("voting_access"), voting_mode: data.get("voting_mode"),
          voting_opens_at: data.get("voting_opens_at") || null, voting_closes_at: data.get("voting_closes_at") || null,
          results_visible: data.get("results_visible") === "on", quadratic_budget: Number(data.get("quadratic_budget") || 16),
        }),
      });
      notify("Event created"); await loadPage("organizer");
    } catch (error) { notify(error.message); }
  });
  const policyForm = document.querySelector("#voting-policy-form");
  if (policyForm) policyForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(policyForm);
    try {
      await api(`/api/organizer/events/${state.events[0].id}/voting`, {
        method: "PUT",
        body: JSON.stringify({
          voting_access: data.get("voting_access"),
          voting_mode: data.get("voting_mode"),
          voting_opens_at: data.get("voting_opens_at") || null,
          voting_closes_at: data.get("voting_closes_at") || null,
          results_visible: data.get("results_visible") === "on",
          quadratic_budget: Number(data.get("quadratic_budget") || 16),
        }),
      });
      notify("Ballot policy saved"); await loadPage("organizer");
    } catch (error) { notify(error.message); }
  });
  const feedbackForm = document.querySelector("#feedback-policy-form");
  if (feedbackForm) feedbackForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(feedbackForm);
    try {
      await api(`/api/organizer/events/${state.events[0].id}/feedback`, {
        method: "PUT",
        body: JSON.stringify({ feedback_visible: data.get("feedback_visible") === "on" }),
      });
      notify("Feedback release policy saved"); await loadPage("organizer");
    } catch (error) { notify(error.message); }
  });
  const awardForm = document.querySelector("#award-form");
  if (awardForm) awardForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(awardForm);
    try {
      await api("/api/organizer/awards", {
        method: "POST",
        body: JSON.stringify({
          event_id: state.events[0].id,
          prize_id: data.get("prize_id") ? Number(data.get("prize_id")) : null,
          project_id: data.get("project_id") ? Number(data.get("project_id")) : null,
          title: data.get("title"),
          recipient_name: data.get("recipient_name") || "",
        }),
      });
      notify("Award saved"); await loadPage("organizer");
    } catch (error) { notify(error.message); }
  });
  const rubricForm = document.querySelector("#rubric-form");
  if (rubricForm) rubricForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const criteria = [...rubricForm.querySelectorAll(".rubric-row")].map((row, index) => ({
      id: Number(row.dataset.criterionId),
      name: row.querySelector("[data-rubric-name]").value.trim(),
      description: row.querySelector("[data-rubric-description]").value.trim(),
      weight: Number(row.querySelector("[data-rubric-weight]").value),
      sort_order: index + 1,
    }));
    try {
      await api(`/api/organizer/events/${state.events[0].id}/criteria`, { method: "PUT", body: JSON.stringify({ criteria }) });
      notify("Rubric saved"); await loadPage("organizer");
    } catch (error) { notify(error.message); }
  });
  const inviteForm = document.querySelector("#invite-form");
  if (inviteForm) inviteForm.addEventListener("submit", async (event) => { event.preventDefault(); const email = new FormData(inviteForm).get("email"); try { await api("/api/organizer/judge-invitations", { method: "POST", body: JSON.stringify({ event_id: state.events[0].id, email }) }); notify("Invitation created"); await loadPage("organizer"); } catch (error) { notify(error.message); } });
  const assignmentForm = document.querySelector("#assignment-form");
  if (assignmentForm) assignmentForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(assignmentForm);
    const projectIds = data.getAll("project_ids").map(Number);
    if (!projectIds.length) { notify("Select at least one submitted project"); return; }
    try {
      const result = await api("/api/organizer/assignments", { method: "POST", body: JSON.stringify({ judge_id: Number(data.get("judge_id")), project_ids: projectIds }) });
      notify(`${result.created} assignment${result.created === 1 ? "" : "s"} created`); await loadPage("organizer");
    } catch (error) { notify(error.message); }
  });
  const autoAssignmentForm = document.querySelector("#auto-assignment-form");
  if (autoAssignmentForm) autoAssignmentForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const reviewsPerProject = Number(new FormData(autoAssignmentForm).get("reviews_per_project") || 2);
    try {
      const result = await api("/api/organizer/assignments/auto", {
        method: "POST",
        body: JSON.stringify({ event_id: state.events[0].id, reviews_per_project: reviewsPerProject }),
      });
      notify(`${result.created} assignments created${result.incomplete_project_ids.length ? `; ${result.incomplete_project_ids.length} projects still need coverage` : ""}`);
      await loadPage("organizer");
    } catch (error) { notify(error.message); }
  });
  const auditSearch = document.querySelector("#audit-search");
  const auditEntity = document.querySelector("#audit-entity");
  const applyAuditFilter = () => {
    const query = auditSearch.value.toLowerCase();
    const entity = auditEntity.value;
    document.querySelectorAll("#audit-log-rows tr").forEach((row) => {
      row.hidden = Boolean(query && !row.textContent.toLowerCase().includes(query)) || Boolean(entity && !row.textContent.toLowerCase().includes(entity.toLowerCase()));
    });
  };
  auditSearch?.addEventListener("input", applyAuditFilter);
  auditEntity?.addEventListener("change", applyAuditFilter);
  document.querySelectorAll(".admin-role").forEach((select) => select.addEventListener("change", async () => {
    try {
      await api(`/api/admin/users/${select.dataset.userId}/role`, {
        method: "PATCH",
        body: JSON.stringify({ role: select.value }),
      });
      notify("User role updated");
      await loadPage("organizer");
    } catch (error) {
      notify(error.message);
      await loadPage("organizer");
    }
  }));
  document.querySelectorAll(".moderation-status").forEach((select) => select.addEventListener("change", async () => {
    try {
      await api(`/api/organizer/comments/${select.dataset.commentId}`, {
        method: "PATCH",
        body: JSON.stringify({ status: select.value }),
      });
      notify("Comment status updated");
      await loadPage("organizer");
    } catch (error) {
      notify(error.message);
    }
  }));
  const importForm = document.querySelector("#import-form");
  if (importForm) importForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const rows = JSON.parse(new FormData(importForm).get("rows"));
      if (!Array.isArray(rows)) throw new Error("Import must be a JSON array");
      const result = await api("/api/organizer/import/projects", { method: "POST", body: JSON.stringify({ event_id: state.events[0].id, rows }) });
      notify(`${result.imported} projects imported${result.rejected.length ? `; ${result.rejected.length} rejected` : ""}`);
      await loadPage("organizer");
    } catch (error) { notify(error.message); }
  });
  const webhookForm = document.querySelector("#webhook-form");
  if (webhookForm) webhookForm.addEventListener("submit", async (event) => {
    event.preventDefault(); const data = new FormData(webhookForm);
    try {
      const result = await api("/api/organizer/webhooks", { method: "POST", body: JSON.stringify({ event_id: state.events[0].id, url: data.get("url"), events: String(data.get("events")).split(",").map((item) => item.trim()).filter(Boolean) }) });
      notify(`Webhook registered. Secret: ${result.secret}`);
      await loadPage("organizer");
    } catch (error) { notify(error.message); }
  });
  const certificateForm = document.querySelector("#certificate-form");
  if (certificateForm) certificateForm.addEventListener("submit", async (event) => {
    event.preventDefault(); const data = new FormData(certificateForm);
    try { const result = await api("/api/organizer/certificates", { method: "POST", body: JSON.stringify({ project_id: Number(data.get("project_id")), recipient_name: data.get("recipient_name"), award_title: data.get("award_title") }) }); notify(`Certificate issued: ${result.certificate.certificate_code}`); await loadPage("organizer"); } catch (error) { notify(error.message); }
  });
  const judgeRecordForm = document.querySelector("#judge-record-form");
  if (judgeRecordForm) judgeRecordForm.addEventListener("submit", async (event) => {
    event.preventDefault(); const data = new FormData(judgeRecordForm);
    try {
      const result = await api("/api/organizer/judge-records", { method: "POST", body: JSON.stringify({ event_id: state.events[0].id, judge_id: Number(data.get("judge_id")) }) });
      notify(`Signed record issued: ${result.record.record_code}`); await loadPage("organizer");
    } catch (error) { notify(error.message); }
  });
}

function render() {
  if (!state.user) return renderLogin();
  const pages = {
    overview: [overviewPage(), "Event overview", "A live read on submissions, judging, and the public surface."],
    gallery: [galleryPage(), "Public gallery", "The submitted work is the source of truth."],
    results: [resultsPage(), "Results", "Published rankings, with the methodology kept visible."],
    submit: [submitPage(), "My submission", "Draft, edit, and publish before the deadline."],
    judge: [judgePage(), "Judge queue", "Your assigned work, and only your assigned work."],
    organizer: [organizerPage(), "Organizer desk", "Configure the event and protect the integrity of the result."],
  };
  const [content, title, subtitle] = pages[state.page] || pages.overview; shell(content, title, subtitle);
  if (state.page === "gallery") bindGallery();
  if (state.page === "submit") bindSubmit();
  if (state.page === "judge") { bindJudge(); if (state.activeProject) renderJudgeDetail(); }
  if (state.page === "organizer") bindOrganizer();
}

boot().catch((error) => { app.innerHTML = `<div class="login-screen"><div class="error">Could not start Hacknight: ${esc(error.message)}</div></div>`; });