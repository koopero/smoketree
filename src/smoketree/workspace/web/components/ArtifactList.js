// The left pane: a flat table of artifact rows. Sortable column headers (click to toggle
// asc/desc), a filter row of native <select>s (one distinct-value dropdown per column), and
// selectable rows. Purely presentational — all state lives in Browser.
export default {
  props: {
    columns: { type: Array, required: true },
    rows: { type: Array, required: true },
    distincts: { type: Object, required: true },
    filters: { type: Object, required: true },
    sort: { type: Object, required: true },
    selectedId: { type: String, default: '' },
  },
  emits: ['select', 'sort', 'filter'],
  methods: {
    cell(row, col) {
      if (col === 'rule') return row.rule;
      if (col === 'state') return row.state;
      if (col === 'updated') return this.fmtTime(row.completed_at);
      return row.cols[col] ?? '';
    },
    fmtTime(iso) {
      if (!iso) return '';
      const m = /T(\d{2}:\d{2})/.exec(iso);
      return m ? m[1] : iso;
    },
    caret(col) {
      if (this.sort.col !== col) return '';
      return this.sort.dir > 0 ? ' ▲' : ' ▼';
    },
  },
  template: `
    <div class="listwrap">
    <table class="arttable">
      <thead>
        <tr>
          <th v-for="col in columns" :key="col" @click="$emit('sort', col)"
              :class="{ sorted: sort.col === col }">{{ col }}{{ caret(col) }}</th>
        </tr>
        <tr class="filterrow">
          <th v-for="col in columns" :key="col">
            <select v-if="col !== 'updated'"
                    :value="filters[col] || ''"
                    @change="$emit('filter', { col, value: $event.target.value })">
              <option value="">all</option>
              <option v-for="v in distincts[col]" :key="v" :value="v">{{ v }}</option>
            </select>
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="row.identity" class="row"
            :class="{ selected: row.identity === selectedId }"
            @click="$emit('select', row.identity)">
          <td v-for="col in columns" :key="col">
            <span v-if="col === 'state'" class="badge" :class="'b-' + row.state"
                  :title="row.reason">{{ row.state }}</span>
            <template v-else>{{ cell(row, col) }}</template>
          </td>
        </tr>
        <tr v-if="!rows.length"><td :colspan="columns.length" class="empty small">No matching artifacts.</td></tr>
      </tbody>
    </table>
    </div>
  `,
};
