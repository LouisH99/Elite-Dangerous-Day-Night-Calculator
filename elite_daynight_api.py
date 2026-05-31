{% extends "admin_base.html" %}
{% block title %}Racing import result · Elite Day/Night Control{% endblock %}
{% block content %}
<section class="card">
  <div class="split">
    <div>
      <h1>Racing import result</h1>
      <p class="muted">Imported {{ result.imported_count }} POIs. Skipped {{ result.skipped_count }} races.</p>
    </div>
    <a class="button" href="/control/racing">Back to racing import</a>
  </div>
</section>

<section class="card wide-card">
  <h2>Imported</h2>
  <div class="table-wrap">
    <table class="dense"><thead><tr><th>Race</th><th>System</th><th>Body</th><th>POI</th></tr></thead><tbody>
    {% for r in result.imported %}
      <tr><td>{{ r.name }}<br><span class="muted">{{ r.key }}</span></td><td>{{ r.system_name }}</td><td>{{ r.body_name }}</td><td>#{{ r.poi_id }}</td></tr>
    {% else %}
      <tr><td colspan="4" class="muted">Nothing imported.</td></tr>
    {% endfor %}
    </tbody></table>
  </div>
</section>

<section class="card wide-card">
  <h2>Skipped</h2>
  <div class="table-wrap">
    <table class="dense"><thead><tr><th>Race</th><th>System</th><th>Body</th><th>Reason</th></tr></thead><tbody>
    {% for r in result.skipped %}
      <tr><td>{{ r.name }}<br><span class="muted">{{ r.key }}</span></td><td>{{ r.system_name|dash }}</td><td>{{ r.body_name|dash }}</td><td>{{ r.reason }}</td></tr>
    {% else %}
      <tr><td colspan="4" class="muted">No skipped races.</td></tr>
    {% endfor %}
    </tbody></table>
  </div>
</section>
{% endblock %}
