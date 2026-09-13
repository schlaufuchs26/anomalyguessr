import React from "react";

export class ErrorBoundary extends React.Component<
  { children: React.ReactNode; fallback?: React.ReactNode },
  { hasError: boolean; error: Error | null }
> {
  constructor(props: {
    children: React.ReactNode;
    fallback?: React.ReactNode;
  }) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }

  render() {
    if (this.state.hasError) {
      return (
        <>
          {this.props.fallback || (
            <div className="error-boundary">
              <div className="error-title">Something went wrong</div>
              <div className="error-detail">
                {this.state.error?.message || "Unknown error"}
              </div>
            </div>
          )}
          <div className="modal-overlay">
            <div className="modal" style={{ maxWidth: 480 }}>
              {this.props.fallback || (
                <>
                  <div className="modal-header">
                    <div className="modal-title">Something went wrong</div>
                  </div>
                  <div className="modal-body">
                    <div className="error-detail" style={{ marginBottom: 0 }}>
                      {this.state.error?.message || "Unknown error"}
                    </div>
                  </div>
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "flex-end",
                      padding: "12px 20px",
                      borderTop: "1px solid var(--border)",
                    }}
                  >
                    <button
                      type="button"
                      className="modal-close"
                      onClick={() =>
                        this.setState({ hasError: false, error: null })
                      }
                    >
                      Dismiss
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        </>
      );
    }
    return this.props.children;
  }
}
