{% extends "admin_base.html" %}
{% block title %}Racing import · Elite Day/Night Control{% endblock %}
{% block content %}
<section class="card">
  <div class="split">
    <div>
      <h1>Razz Racing POI import</h1>
      <p class="muted">Admin-only importer for Elite Dangerous racing starts. Surface starts are imported as POIs and stay hidden until reviewed unless you explicitly mark them public.</p>
    </div>
    <a class="button" href="/control/pois">POIs</a>
  </div>
</section>

<section class="card">
  <h2>Preview races</h2>
  <form method="get" action="/control/racing" class="inline-form wrap">
    <input type="hidden" name="preview" value="1">
    <label>Preview limit
      <input name="limit" type="number" min="1" max="200" value="{{ limit }}">
    </label>
    <button type="submit">Fetch preview</button>
  </form>
  <p class="muted">Preview fetches race details to read the start body and surface coordinates. Some races may be skipped if their first waypoint is space-based or does not include lat/lon.</p>
</section>

{% if results is not none %}
<section class="card wide-card">
  <h2>Preview results</h2>
  <div class="table-wrap">
    <table class="dense admin-review-table">
      <thead><tr><th>Race</th><th>System</th><th>Body</th><th>Start coords</th><th>Status</th></tr></thead>
      <tbody>
      {% for r in results %}
        <tr>
          <td><strong>{{ r.name }}</strong><br><span class="muted">{{ r.key }}</span></td>
          <td>{{ r.system_name|dash }}</td>
          <td>{{ r.body_name|dash }}</td>
          <td>{% if r.surface_start %}{{ r.lat|num(6) }}, {{ r.lon|num(6) }}{% else %}—{% endif %}</td>
          <td>{% if r.surface_start %}<span class="badge badge-ok">surface start</span>{% else %}<span class="badge badge-muted">{{ r.reason|dash }}</span>{% endif %}</td>
        </tr>
        {% if r.description or r.start_note %}
        <tr class="review-detail-row"><td colspan="5">
          <div class="review-detail-grid single">
            {% if r.description %}<div><b>Description</b><p>{{ r.description }}</p></div>{% endif %}
            {% if r.start_note %}<div><b>Start note</b><p>{{ r.start_note }}</p></div>{% endif %}
          </div>
        </td></tr>
        {% endif %}
      {% else %}
        <tr><td colspan="5" class="muted">No preview results.</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</section>
{% endif %}

<section class="card">
  <h2>Import surface starts</h2>
  <form method="post" action="/control/racing/import" class="stack-form">
    <label>Import limit
      <input name="limit" type="number" min="1" max="500" value="100">
      <span class="field-help">The importer fetches one detail call per race, so large imports can take a while on a Raspberry Pi.</span>
    </label>
    <label>Status for imported POIs
      <select name="review_status">
        <option value="needs_check" selected>needs_check</option>
        <option value="new">new</option>
        <option value="approved">approved</option>
      </select>
    </label>
    <label class="check"><input type="checkbox" name="import_missing_systems"> Import missing systems/bodies from Spansh when needed</label>
    <label class="check"><input type="checkbox" name="make_public"> Make imported POIs public immediately</label>
    <button type="submit" class="primary">Import racing POIs</button>
  </form>
</section>
{% endblock %}
