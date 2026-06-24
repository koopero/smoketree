import { ref, onMounted } from 'vue';
import { postJSON, escapeHtml } from '../util.js';

// Surfaces authored copies that drifted from their generated template, with a
// colored unified diff and reconcile actions (merge / take generated / keep mine).
export default {
  emits: ['reconciled'],
  setup(props, { emit, expose }) {
    const drift = ref([]);
    const busy = ref('');  // id of a row currently resolving

    async function refresh() {
      const data = await (await fetch('/api/drift')).json();
      drift.value = data.drift || [];
    }

    async function resolve(id, action) {
      busy.value = id;
      try {
        const r = await fetch('/api/reconcile', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id, action }),
        });
        if (r.ok) {
          await refresh();
          emit('reconciled');  // ask the grid to re-render too
        }
      } finally {
        busy.value = '';
      }
    }

    // Color a unified diff: + lines green, - lines red (skip the +++/--- headers).
    function colorDiff(diff) {
      return diff.split('\n').map((l) => {
        const e = escapeHtml(l);
        if (l.startsWith('+') && !l.startsWith('+++')) return `<span class="add">${e}</span>`;
        if (l.startsWith('-') && !l.startsWith('---')) return `<span class="del">${e}</span>`;
        return e;
      }).join('\n');
    }

    onMounted(refresh);
    expose({ refresh });
    return { drift, busy, resolve, colorDiff };
  },
  template: `
    <section v-if="drift.length" class="drift">
      <h2>⚠ {{ drift.length }} authored cop(ies) drifted from their template</h2>
      <div v-for="d in drift" :key="d.id" class="drow">
        <div class="top">
          <span class="path">{{ d.id }}</span>
          <span v-if="d.edited" class="edited">you edited it</span>
          <span class="acts">
            <button v-if="d.is_text" type="button" :disabled="busy === d.id"
                    @click="resolve(d.id, 'merge')">merge</button>
            <button type="button" :disabled="busy === d.id"
                    @click="resolve(d.id, 'take-generated')">take generated</button>
            <button type="button" :disabled="busy === d.id"
                    @click="resolve(d.id, 'keep-mine')">keep mine</button>
          </span>
        </div>
        <pre v-html="colorDiff(d.diff)"></pre>
      </div>
    </section>
  `,
};
