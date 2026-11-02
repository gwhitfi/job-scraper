"""The add-company form; extraction details appear only when automatic discovery needs help."""

FORM = """
<dialog id="add-company">
  <h2>Add company to track</h2>
  <form id="company-form">
    <label>Careers or jobs URL<input name="url" required placeholder="careers.example.com"></label>
    <label>Company name (optional)
        <input name="name" maxlength="120"></label>
    <label><input name="is_board" type="checkbox"> This site lists multiple employers</label>
    <label>Locations<select name="locations">{locations}</select></label>
    <p class="meta">Remote jobs are included. Tracking starts with the next scheduled check.</p>
    <details id="company-advanced">
      <summary>Help with a page that wasn't recognized</summary>
      <p>For an HTML page, right-click a job → Inspect. Find the repeated card and copy
      its CSS selector. Title and link selectors are relative to that card.</p>
      <label>Format<select name="format">
        <option value="">Detect automatically</option><option value="html">HTML job cards</option>
        <option value="json">Public JSON jobs feed</option>
        <option value="jsonld">Structured job data</option>
      </select></label>
      <label>Job card selector / JSON jobs-list field
        <input name="items" placeholder=".job-card or data.jobs"></label>
      <label>Title selector / field
        <input name="title" placeholder="h2 or title"></label>
      <label>Link selector / field
        <input name="link" placeholder="a@href or url"></label>
      <label>Stable job ID selector / field (optional)
        <input name="source_id" placeholder="@data-job-id or id"></label>
      <p class="meta">No separate job links? Use $page for Link and $text for Stable job ID
      when each job card contains only its title.</p>
      <label>Location selector / field (optional)
        <input name="location" placeholder=".location or location.name"></label>
      <label>Employer selector / field (for job boards)
        <input name="employer" placeholder=".employer or company.name"></label>
      <label>Country selector / field (optional)
        <input name="countries" placeholder=".country or location.country"></label>
      <label>Next-page link selector / JSON field (optional)
        <input name="next" placeholder="a.next or links.next"></label>
      <label>JSON request method<select name="method">
        <option>GET</option><option>POST</option></select></label>
      <label>Public JSON request body (POST only)
        <textarea name="body" rows="3" placeholder='{{"searchText": "", "limit": 20}}'></textarea>
      </label>
      <label>Page or offset field (JSON only)<input name="page_field" placeholder="offset"></label>
      <label>First page / offset<input name="page_start" type="number" min="0" value="0"></label>
      <label>Page / offset increment
        <input name="page_step" type="number" min="1" value="20"></label>
      <label>JSON total-count field (optional)
        <input name="total" placeholder="totalCount"></label>
      <label>Exact no-openings message (optional)
        <input name="none_text"></label>
      <p class="meta">For browser-loaded jobs: F12 → Network → Fetch/XHR → reload. Find the response
      containing job titles and copy its public Request URL above.
      Public GET and JSON POST feeds are supported;
      do not paste cookies, passwords, or authorization headers.</p>
    </details>
    <div id="company-result" role="status" aria-live="polite"></div>
    <div class="dialog-actions">
      <button type="button" class="btn" id="close-company">Close</button>
      <button type="submit" class="btn primary">Add and start tracking</button>
    </div>
  </form>
</dialog>
"""

SCRIPT = """
const companyDialog = document.getElementById("add-company");
const companyForm = document.getElementById("company-form");
const companyResult = document.getElementById("company-result");
document.getElementById("open-company").addEventListener("click", () => companyDialog.showModal());
document.getElementById("close-company").addEventListener("click", () => companyDialog.close());
companyForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = companyForm.querySelector('[type="submit"]');
  button.disabled = true;
  companyResult.textContent = "Checking the job list… This may take a minute.";
  const fields = Object.fromEntries(new FormData(companyForm));
  const body = {url: fields.url, name: fields.name, locations: fields.locations,
                is_board: fields.is_board === "on"};
  try {
  if (fields.format) {
    body.settings = {format: fields.format};
    if (fields.format !== "jsonld") {
      body.settings[fields.format === "html" ? "item" : "items"] = fields.items;
      body.settings.fields = {title: fields.title, url: fields.link};
      if (fields.source_id) body.settings.fields.source_id = fields.source_id;
      for (const key of ["location", "employer", "countries"]) {
        if (fields[key]) body.settings.fields[key] = fields[key];
      }
    }
    if (fields.format === "json") {
      body.settings.method = fields.method;
      body.settings.body = JSON.parse(fields.body || "{}");
      if (fields.page_field) {
        body.settings.page_field = fields.page_field;
        body.settings.page_start = Number(fields.page_start);
        body.settings.page_step = Number(fields.page_step);
      }
    }
    for (const key of ["next", "total", "none_text"]) {
      if (fields[key]) body.settings[key] = fields[key];
    }
  }
    const response = await fetch("/api/companies", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Requested-With": "jobscraper"},
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(await response.text());
    const result = await response.json();
    companyResult.replaceChildren();
    const message = document.createElement("p");
    message.textContent = result.message;
    companyResult.append(message);
    if (result.log_url) {
      const link = document.createElement("a");
      link.href = result.log_url;
      link.textContent = "View attempt log";
      link.target = "_blank";
      companyResult.append(link);
    }
    for (const tip of result.help || []) {
      const paragraph = document.createElement("p");
      paragraph.textContent = tip;
      companyResult.append(paragraph);
    }
    if (result.status === "added") {
      const list = document.createElement("ul");
      for (const job of result.sample) {
        const item = document.createElement("li");
        item.textContent = job.title + (job.location ? " — " + job.location : "");
        list.append(item);
      }
      companyResult.append(list);
      companyForm.reset();
    } else {
      document.getElementById("company-advanced").open = result.status !== "blocked";
    }
  } catch (error) {
    companyResult.textContent = "Could not add company: " + error.message;
    const link = document.createElement("a");
    link.href = "/logs";
    link.textContent = " View attempt logs";
    link.target = "_blank";
    companyResult.append(link);
    document.getElementById("company-advanced").open = true;
  } finally {
    button.disabled = false;
  }
});
"""
