// Synthetic response shapes, not recordings or a claim of current live YouTube compatibility.
export const target = {
  channelId: 'channel:metrics-test', sourceChannelId: 'UC' + 'a'.repeat(22),
  sourceContentId: 'abcdefghijk', contentType: 'video',
};
export function metricsEvidence() {
  return {
    requestedVideoId: target.sourceContentId, observedAt: '2026-09-20T01:00:00.000Z',
    player: {
      playabilityStatus: { status: 'OK' },
      videoDetails: { videoId: target.sourceContentId, channelId: target.sourceChannelId,
        title: 'Local fixture video', lengthSeconds: '10', viewCount: '9007199254740993',
        thumbnail: { thumbnails: [] }, author: 'Fixture', shortDescription: '' },
    },
    next: {
      currentVideoEndpoint: { watchEndpoint: { videoId: target.sourceContentId } },
      contents: { twoColumnWatchNextResults: {
        results: { results: { contents: [
          { videoPrimaryInfoRenderer: { title: { simpleText: 'Local fixture video' },
            videoActions: { menuRenderer: { topLevelButtons: [
              { toggleButtonRenderer: { defaultIcon: { iconType: 'LIKE' },
                  defaultText: { simpleText: '1.2K', accessibility: { accessibilityData: {
                    label: 'like this video along with 1,234 other people',
                  } } } } },
            ] } },
          } },
          { itemSectionRenderer: { targetId: 'comments-entry-point', contents: [
            { commentsEntryPointHeaderRenderer: { commentCount: { simpleText: '5,678' } } },
          ] } },
        ] } },
        secondaryResults: { secondaryResults: { results: [] } },
      } },
    },
  };
}
export const mainContents = evidence => evidence.next.contents.twoColumnWatchNextResults.results.results.contents;
export const likeButton = evidence => mainContents(evidence)[0].videoPrimaryInfoRenderer.videoActions.menuRenderer.topLevelButtons[0]
  .toggleButtonRenderer;
export const commentsSection = evidence => mainContents(evidence)[1].itemSectionRenderer;
