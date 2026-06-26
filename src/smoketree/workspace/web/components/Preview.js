import { ref, watchEffect } from 'vue';

// Renders a card's output by media type. Images (now including SVG) and video
// render directly from /artifact; text/data are fetched and shown escaped in a
// <pre>; anything else falls back to a download link.
export default {
  props: {
    card: { type: Object, required: true },
    bust: { type: Number, required: true },
  },
  setup(props) {
    const text = ref(null);     // fetched text/data body
    const failed = ref(false);

    watchEffect(async () => {
      const { card, bust } = props;
      const url = `${card.artifact_url}&t=${bust}`;
      if (card.media === 'text' || card.media === 'data') {
        text.value = null;
        failed.value = false;
        try {
          text.value = await (await fetch(url)).text();
        } catch {
          failed.value = true;
        }
      }
    });

    return { text, failed };
  },
  computed: {
    url() {
      return `${this.card.artifact_url}&t=${this.bust}`;
    },
    // Small cached thumbnail for the grid (full-res only on click) — avoids decoding many
    // 1024px+ PNGs at once. /artifact?id=… and /file?path=… both map to /thumb?….
    thumbUrl() {
      const t = this.card.artifact_url
        .replace('/artifact?', '/thumb?')
        .replace('/file?', '/thumb?');
      return `${t}&t=${this.bust}`;
    },
    isText() {
      return this.card.media === 'text' || this.card.media === 'data';
    },
  },
  template: `
    <a v-if="card.media === 'image'" :href="url" target="_blank" rel="noopener" title="open full size">
      <img loading="lazy" :src="thumbUrl">
    </a>
    <video v-else-if="card.media === 'video'" :src="url" controls loop muted playsinline></video>
    <template v-else-if="isText">
      <pre v-if="text !== null">{{ text }}</pre>
      <div v-else-if="failed" class="none">could not load output</div>
    </template>
    <div v-else class="none"><a :href="url">download {{ card.media }}</a></div>
  `,
};
