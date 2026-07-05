// Shared player state, read/written by both player.js (which owns playback)
// and rows.js (which needs to know which item is active while rendering
// rows/tiles, and to update the active-row reference when it builds one).
// A plain mutable object rather than individually exported `let`s: ES module
// bindings can't be reassigned from an importing module, only read, so
// sharing mutable state across modules means either a getter/setter pair per
// field or one object whose properties every module mutates directly -- the
// object is less boilerplate for three fields that always change together.
export const playerState = {
  activePlayingId: null,
  activeRowBtn: null,
  activeHls: null,
};
