import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import { SceneRenderer } from "../render/SceneRenderer.js";

// Owns the canvas and the SceneRenderer. React never touches the 3D scene except through this component.
const Viewport = forwardRef(function Viewport({ doc, options, selectedIndex, onHover, onSelect, onTiles }, ref) {
  const canvasRef = useRef(null);
  const labelRef = useRef(null);
  const engine = useRef(null);
  const handlers = useRef({});
  handlers.current = { onHover, onSelect, onTiles };

  // One renderer for the life of the component; callbacks go through a ref so they stay current.
  useEffect(() => {
    const renderer = new SceneRenderer(canvasRef.current, {
      onHover: (i, point) => handlers.current.onHover?.(i, point),
      onSelect: (i) => handlers.current.onSelect?.(i),
      onTiles: (s) => handlers.current.onTiles?.(s),
    });
    renderer.setWellLabel(labelRef.current);
    engine.current = renderer;
    return () => renderer.dispose();
  }, []);

  useEffect(() => {
    if (doc) engine.current.setSite(doc);
  }, [doc]);

  useEffect(() => {
    engine.current.setOptions(options);
  }, [options, doc]);

  useEffect(() => {
    engine.current.setSelected(selectedIndex);
  }, [selectedIndex, doc]);

  useImperativeHandle(ref, () => ({ resetView: () => engine.current.resetView() }));

  return (
    <div className="viewport">
      <canvas ref={canvasRef} aria-label="3D map of farm parcels around the tube-well" role="img" />
      <div ref={labelRef} className="well-label">
        Tube-well · {doc ? doc.radiusM : 150} m command zone
      </div>
    </div>
  );
});

export default Viewport;
