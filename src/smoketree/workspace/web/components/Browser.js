import { ref, computed, watch, onMounted, reactive } from 'vue';
import { postJSON, cardId } from '../util.js';
import ArtifactList from './ArtifactList.js';
import ArtifactDetail from './ArtifactDetail.js';

// The two-pane artifact browser. Fetches /api/graph, flattens every rule's instances into
// one row list, and owns filter + sort + selection state. Left: a sortable, key-filterable
// table (ArtifactList). Right: properties + actions for the selected row (ArtifactDetail).
// Pipeline runs are delegated upward to RunBar via the `run` event.
export default {
  components: { ArtifactList, ArtifactDetail },
  props: {
    bust: { type: Number, required: true },
  },
  emits: ['run'],
  setup(props, { emit }) {
    const rules = ref([]);
    const error = ref('');
    const loaded = ref(false);

    const filters = reactive({});        // column name -> selected value ('' = all)
    const query = ref('');               // free-text search
    const sort = reactive({ col: '', dir: 1 });   // col '' = natural (execution) order
    const selectedId = ref('');
    const firing = ref('');

    async function load() {
      const data = await (await fetch('/api/graph')).json();
      error.value = data.error || '';
      rules.value = data.error ? [] : data.rules;
      loaded.value = true;
    }

    // rule name -> execution-order index, for sorting/grouping the `rule` column.
    const ruleOrder = computed(() => {
      const m = {};
      rules.value.forEach((r, i) => { m[r.name] = i; });
      return m;
    });

    // Flatten instances to rows. A computed so editing a select in the detail pane (which
    // mutates the shared inst.channels) re-derives the affected row's status cell live.
    const rows = computed(() => {
      const out = [];
      for (const rule of rules.value) {
        for (const inst of rule.instances) {
          const sel = {};
          for (const ch of inst.channels) if (ch.kind === 'select') sel[ch.name] = ch.value;
          out.push({
            inst,
            rule: inst.rule,
            identity: inst.identity,
            label: inst.label,
            state: inst.state,
            reason: inst.reason,
            completed_at: inst.completed_at,
            media: inst.media,
            artifact_url: inst.artifact_url,
            reroll: inst.reroll,
            deps: rule.deps,
            trigger: rule.trigger,
            cardId: cardId(inst),
            cols: { ...inst.keys, ...sel },
          });
        }
      }
      return out;
    });

    // Column order: rule, then binding keys (first-appearance), then select channels, then
    // state + updated. Keys vs selects tracked separately so keys come first globally.
    const columns = computed(() => {
      const keyCols = [];
      const selCols = [];
      for (const rule of rules.value) {
        for (const inst of rule.instances) {
          for (const k of Object.keys(inst.keys)) if (!keyCols.includes(k)) keyCols.push(k);
          for (const ch of inst.channels) {
            if (ch.kind === 'select' && !selCols.includes(ch.name)) selCols.push(ch.name);
          }
        }
      }
      return ['rule', ...keyCols, ...selCols, 'state', 'updated'];
    });

    function cellValue(row, col) {
      if (col === 'rule') return row.rule;
      if (col === 'state') return row.state;
      if (col === 'updated') return row.completed_at || '';
      return row.cols[col] ?? '';
    }

    // Distinct values per column (for the filter dropdowns); 'updated' is not filterable.
    const distincts = computed(() => {
      const m = {};
      for (const col of columns.value) {
        if (col === 'updated') continue;
        const vals = new Set();
        for (const row of rows.value) {
          const v = cellValue(row, col);
          if (v !== '') vals.add(v);
        }
        m[col] = [...vals].sort();
      }
      return m;
    });

    const visibleRows = computed(() => {
      const q = query.value.trim().toLowerCase();
      let out = rows.value.filter((row) => {
        for (const [col, val] of Object.entries(filters)) {
          if (val && cellValue(row, col) !== val) return false;
        }
        if (q) {
          const hay = (row.rule + ' ' + row.label + ' '
            + Object.values(row.cols).join(' ')).toLowerCase();
          if (!hay.includes(q)) return false;
        }
        return true;
      });
      if (sort.col) {
        const key = (row) => sort.col === 'rule'
          ? ruleOrder.value[row.rule]
          : cellValue(row, sort.col);
        out = [...out].sort((a, b) => {
          const ka = key(a), kb = key(b);
          if (ka < kb) return -sort.dir;
          if (ka > kb) return sort.dir;
          return 0;
        });
      }
      return out;
    });

    const selectedRow = computed(() =>
      visibleRows.value.find((r) => r.identity === selectedId.value) || visibleRows.value[0] || null);

    const triggers = computed(() => rules.value
      .filter((r) => r.trigger)
      .map((r) => ({ name: r.name, describe: r.trigger.describe })));

    function onSort(col) {
      if (sort.col === col) sort.dir = -sort.dir;
      else { sort.col = col; sort.dir = 1; }
    }
    function onFilter({ col, value }) { filters[col] = value; }
    function onSelect(id) { selectedId.value = id; }

    function onRun(payload) { emit('run', payload); }

    async function generate(name) {
      if (firing.value) return;
      firing.value = name;
      try {
        await postJSON('/api/trigger', { rule: name });
        emit('run');
      } finally {
        firing.value = '';
      }
    }

    onMounted(load);
    watch(() => props.bust, load);
    // keep a valid selection as rows change
    watch(visibleRows, (rowsNow) => {
      if (!rowsNow.some((r) => r.identity === selectedId.value)) {
        selectedId.value = rowsNow.length ? rowsNow[0].identity : '';
      }
    });

    return {
      error, loaded, columns, distincts, filters, query, sort, visibleRows,
      selectedRow, selectedId, triggers, firing,
      onSort, onFilter, onSelect, onRun, generate,
    };
  },
  template: `
    <div class="browsertoolbar">
      <input class="search" type="search" placeholder="Search…" v-model="query">
      <button v-for="t in triggers" :key="t.name" class="gen" type="button"
              :disabled="!!firing" :title="t.describe || ''" @click="generate(t.name)">
        {{ firing === t.name ? '… generating' : '✦ Generate more — ' + t.name }}
      </button>
      <span class="count">{{ visibleRows.length }} artifact(s)</span>
    </div>
    <div v-if="error" class="empty">⚠ {{ error }}</div>
    <div v-else-if="!loaded" class="empty">Loading…</div>
    <div v-else class="browser">
      <ArtifactList class="listpane"
        :columns="columns" :rows="visibleRows" :distincts="distincts"
        :filters="filters" :sort="sort" :selected-id="selectedRow ? selectedRow.identity : ''"
        @select="onSelect" @sort="onSort" @filter="onFilter" />
      <ArtifactDetail class="detailpane"
        :row="selectedRow" :bust="bust"
        @run="onRun" @flagchange="() => {}" />
    </div>
  `,
};
