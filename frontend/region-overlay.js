/*
 * Draw stored regions over a rendered page.
 *
 * The minimum frontend seam for this phase: it proves that what the backend
 * persists can be put back on the page it came from, which is the round trip
 * the whole region contract exists to guarantee. It does NOT edit anything --
 * the crop editor is untouched and keeps working exactly as before.
 *
 * WHY THIS IS SO SHORT
 * --------------------
 * Because geometry is normalised to the page (0..1), placing a region is a
 * percentage, not a calculation. There is no scale factor to track, no device
 * pixel ratio, nothing to recompute when the container resizes or the PDF is
 * re-rendered at a different zoom. Had coordinates been stored in pixels this
 * file would need the exact viewport the annotation was made at, and would get
 * it wrong on any other screen.
 *
 * A future editor hooks in here: the same elements gain drag handles, and
 * `regionToGeometry` below is the inverse conversion it will need.
 */

const REGION_COLOURS = {
  handwritten_text: '#2f6fed',
  printed_text: '#6b7280',
  math: '#7c3aed',
  diagram: '#0f9d58',
  table: '#0891b2',
  crossed_out: '#b45309',
  teacher_marking: '#dc2626',
  page_furniture: '#9ca3af',
  other: '#4b5563'
};

/* Types that are NOT the student's own answer. Rendered dashed so a reviewer
 * can see at a glance that the teacher's red pen and struck-out working are
 * represented but are not being treated as answer content. */
const NON_ANSWER_TYPES = new Set([
  'crossed_out', 'teacher_marking', 'page_furniture', 'printed_text'
]);

function colourFor(regionType) {
  return REGION_COLOURS[regionType] || REGION_COLOURS.other;
}

/* Fetch every stored region for one answer script. */
async function fetchRegions(answerScriptId, pageIndex) {
  const query = pageIndex === undefined ? '' : `?page_index=${pageIndex}`;
  const response = await authFetch(`/answer-scripts/${answerScriptId}/regions${query}`);
  if (!response.ok) {
    console.error('Could not load regions:', response.status);
    return [];
  }
  const body = await response.json();
  return body.regions || [];
}

/* One region as an absolutely-positioned element inside a page overlay.
 * Percentages, because the geometry is a fraction of the page. */
function renderRect(region) {
  const { x, y, w, h } = region.geometry;
  const box = document.createElement('div');
  box.className = 'region-box';
  box.dataset.regionId = region.id;
  box.dataset.regionType = region.region_type;
  box.dataset.status = region.status;
  box.style.position = 'absolute';
  box.style.left = `${x * 100}%`;
  box.style.top = `${y * 100}%`;
  box.style.width = `${w * 100}%`;
  box.style.height = `${h * 100}%`;
  box.style.border = `2px ${NON_ANSWER_TYPES.has(region.region_type) ? 'dashed' : 'solid'} ${colourFor(region.region_type)}`;
  box.style.boxSizing = 'border-box';
  box.style.pointerEvents = 'none';
  /* A proposal is not an annotation: show it faded until a human accepts it. */
  box.style.opacity = region.status === 'proposed' ? '0.55' : '1';
  return box;
}

/* Polygons need real geometry, so an inline SVG scaled to the page box. */
function renderPolygon(region) {
  const svgNs = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNs, 'svg');
  svg.setAttribute('viewBox', '0 0 1 1');
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.style.position = 'absolute';
  svg.style.left = '0';
  svg.style.top = '0';
  svg.style.width = '100%';
  svg.style.height = '100%';
  svg.style.pointerEvents = 'none';
  svg.dataset.regionId = region.id;

  const polygon = document.createElementNS(svgNs, 'polygon');
  polygon.setAttribute('points', region.geometry.points.map(p => `${p[0]},${p[1]}`).join(' '));
  polygon.setAttribute('fill', 'none');
  polygon.setAttribute('stroke', colourFor(region.region_type));
  /* Stroke is in viewBox units, so it must be tiny to look like 2px. */
  polygon.setAttribute('stroke-width', '0.004');
  if (NON_ANSWER_TYPES.has(region.region_type)) {
    polygon.setAttribute('stroke-dasharray', '0.012 0.008');
  }
  polygon.setAttribute('opacity', region.status === 'proposed' ? '0.55' : '1');
  svg.appendChild(polygon);
  return svg;
}

/* A small caption so the semantics are visible rather than inferred from a
 * colour: the type, the question it is assigned to (or that it is not), and
 * its reading position. None of this is burned into pixels. */
function renderLabel(region) {
  const label = document.createElement('div');
  label.className = 'region-label';
  const bounds = region.geometry_kind === 'rect'
    ? region.geometry
    : boundsOfPolygon(region.geometry.points);
  label.style.position = 'absolute';
  label.style.left = `${bounds.x * 100}%`;
  label.style.top = `${bounds.y * 100}%`;
  label.style.transform = 'translateY(-100%)';
  label.style.font = '11px system-ui, sans-serif';
  label.style.background = colourFor(region.region_type);
  label.style.color = '#fff';
  label.style.padding = '1px 4px';
  label.style.whiteSpace = 'nowrap';
  label.style.pointerEvents = 'none';

  const question = region.question_id
    ? `Q${region.question_id}${region.question_part ? '.' + region.question_part : ''}`
    : 'unassigned';
  label.textContent = `${region.reading_order + 1}. ${region.region_type} · ${question}`;
  return label;
}

function boundsOfPolygon(points) {
  const xs = points.map(p => p[0]);
  const ys = points.map(p => p[1]);
  return { x: Math.min(...xs), y: Math.min(...ys) };
}

/*
 * Draw `regions` into `overlayElement`, which must be positioned and sized to
 * the rendered page (the crop editor's existing `.overlay` already is).
 * Additive: existing children are left alone, only region elements are removed
 * and redrawn.
 */
function renderRegions(overlayElement, regions) {
  overlayElement.querySelectorAll('.region-box, .region-label, svg[data-region-id]')
    .forEach(node => node.remove());

  regions
    .slice()
    /* Reading order is explicit and persisted; never trust DOM order. */
    .sort((a, b) => a.reading_order - b.reading_order)
    .forEach(region => {
      if (region.status === 'rejected') return;   // kept as a record, not shown
      overlayElement.appendChild(
        region.geometry_kind === 'polygon' ? renderPolygon(region) : renderRect(region)
      );
      overlayElement.appendChild(renderLabel(region));
    });
}

/* The inverse conversion a future editor needs: a pixel box drawn on a page
 * element becomes normalised geometry the API will accept. */
function regionToGeometry(pixelBox, pageElement) {
  const width = pageElement.clientWidth;
  const height = pageElement.clientHeight;
  return {
    x: pixelBox.x / width,
    y: pixelBox.y / height,
    w: pixelBox.w / width,
    h: pixelBox.h / height
  };
}

if (typeof window !== 'undefined') {
  window.CogniGradeRegions = {
    fetchRegions, renderRegions, regionToGeometry, colourFor, NON_ANSWER_TYPES
  };
}
