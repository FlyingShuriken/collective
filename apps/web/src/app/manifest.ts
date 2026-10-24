import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    id: "/", name: "Orbyte Collective", short_name: "Orbyte",
    description: "Restocking with clear costs and your approval.",
    start_url: "/", scope: "/", display: "standalone",
    background_color: "#f6f5ed", theme_color: "#183d34",
    icons: [
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}
