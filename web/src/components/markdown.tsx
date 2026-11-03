import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Renders untrusted Markdown (LLM or rule text). Raw HTML is dropped (no rehype-raw), images are not rendered,
 * and react-markdown's default urlTransform strips javascript:/data: links.
 */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        disallowedElements={["img"]}
        unwrapDisallowed
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer nofollow">
              {children}
            </a>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
