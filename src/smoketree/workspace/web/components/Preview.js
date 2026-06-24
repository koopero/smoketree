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
    isText() {
      return this.card.media === 'text' || this.card.media === 'data';
    },
  },
  template: `
    <img v-if="card.media === 'image'" loading="lazy" :src="url">
    <video v-else-if="card.media === 'video'" :src="url" controls loop muted playsinline></video>
    <template v-else-if="isText">
      <pre v-if="text !== null">{{ text }}</pre>
      <div v-else-if="failed" class="none">could not load output</div>
    </template>
    <div v-else class="none"><a :href="url">download {{ card.media }}</a></div>
  `,
};
