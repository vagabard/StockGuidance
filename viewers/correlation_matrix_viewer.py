import streamlit as st
import pandas as pd
import numpy as np
import seaborn as sns
import matplotlib.pyplot as plt
from pathlib import Path

def main():
    st.set_page_config(page_title="Ticker Correlation Matrix Viewer", layout="wide")
    st.title("Ticker Correlation Matrix Viewer")

    file_path = st.sidebar.text_input("File Path (CSV or Parquet)", "ticker_correlations.parquet" if Path("ticker_correlations.parquet").exists() else "ticker_correlations.csv")

    if not Path(file_path).exists():
        st.error(f"File not found: {file_path}")
        return

    @st.cache_data
    def load_data(path):
        if path.endswith('.parquet'):
            return pd.read_parquet(path)
        return pd.read_csv(path, index_col=0)

    try:
        corr_matrix = load_data(file_path)
    except Exception as e:
        st.error(f"Error loading file: {e}")
        return

    st.sidebar.header("Filters")
    tickers = corr_matrix.columns.tolist()
    selected_tickers = st.sidebar.multiselect("Select Tickers", tickers, default=tickers[:min(20, len(tickers))])

    if not selected_tickers:
        st.warning("Please select at least one ticker.")
        return

    filtered_corr = corr_matrix.loc[selected_tickers, selected_tickers]

    st.subheader("Correlation Heatmap (Triangular)")

    # Create a mask for the upper triangle
    mask = np.triu(np.ones_like(filtered_corr, dtype=bool))

    fig, ax = plt.subplots(figsize=(10, 8))
    sns.heatmap(
        filtered_corr,
        mask=mask,
        annot=len(selected_tickers) <= 15,
        fmt=".2f",
        cmap='coolwarm',
        vmin=-1, vmax=1,
        center=0,
        square=True,
        linewidths=.5,
        cbar_kws={"shrink": .8},
        ax=ax
    )
    plt.xticks(rotation=45, ha='right')
    plt.yticks(rotation=0)
    st.pyplot(fig)

    if st.checkbox("Show Raw Data"):
        st.dataframe(filtered_corr)

    st.sidebar.markdown("---")
    st.sidebar.write(f"Total tickers in file: {len(tickers)}")

    st.header("Find Highly Correlated Stocks")

    threshold = st.slider("Absolute Correlation Threshold", 0.0, 1.0, 0.9, 0.01)

    # We want to find pairs (A, B) where |corr(A, B)| >= threshold and A != B
    # Since it's a symmetric matrix, we only need the upper triangle

    # Extract the upper triangle without the diagonal
    upper_tri = corr_matrix.where(np.triu(np.ones(corr_matrix.shape), k=1).astype(bool))

    # Stack to get pairs
    pairs = upper_tri.stack().reset_index()
    pairs.columns = ['Ticker A', 'Ticker B', 'Correlation']

    # Filter by absolute threshold
    filtered_pairs = pairs[pairs['Correlation'].abs() >= threshold].copy()

    st.write(f"Number of pairs with absolute correlation >= {threshold}: **{len(filtered_pairs)}**")

    if not filtered_pairs.empty:
        st.dataframe(filtered_pairs.sort_values(by='Correlation', ascending=False))

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Save to CSV"):
                output_name = f"high_correlation_{threshold}.csv"
                filtered_pairs.to_csv(output_name, index=False)
                st.success(f"Saved to {output_name}")
        with col2:
            if st.button("Save to Parquet"):
                output_name = f"high_correlation_{threshold}.parquet"
                filtered_pairs.to_parquet(output_name, index=False)
                st.success(f"Saved to {output_name}")

    st.header("Correlation Classes")
    if not filtered_pairs.empty:
        # Build adjacency list for connected components
        adj = {}
        for _, row in filtered_pairs.iterrows():
            a, b = row['Ticker A'], row['Ticker B']
            if a is None or b is None:
                continue
            if a not in adj: adj[a] = []
            if b not in adj: adj[b] = []
            adj[a].append(b)
            adj[b].append(a)

        visited = set()
        classes = []

        # We need to find connected components
        # For each stock in adj, if not visited, start BFS/DFS
        all_stocks = sorted([s for s in adj.keys() if s is not None])
        for stock in all_stocks:
            if stock not in visited:
                component = []
                stack = [stock]
                visited.add(stock)
                while stack:
                    curr = stack.pop()
                    component.append(curr)
                    for neighbor in adj.get(curr, []):
                        if neighbor not in visited:
                            visited.add(neighbor)
                            stack.append(neighbor)

                # For this component (class), find the minimum correlation score among its members
                # that are in filtered_pairs
                class_set = set(component)
                class_corrs = filtered_pairs[
                    filtered_pairs['Ticker A'].isin(class_set) &
                    filtered_pairs['Ticker B'].isin(class_set)
                ]['Correlation'].abs()

                min_corr = class_corrs.min() if not class_corrs.empty else threshold

                # Calculate Centroid
                # For each member M, sum(1 - abs(corr(M, other))) for all other in class
                centroid = None
                if len(component) > 1:
                    min_sum_dist = float('inf')
                    for m in component:
                        sum_dist = 0
                        for other in component:
                            if m == other:
                                continue
                            # Use the full corr_matrix to get the correlation between any two members
                            # Use .abs() as requested by the formula pattern (1 - correlation)
                            # implying we care about distance from perfect correlation
                            correlation = abs(corr_matrix.loc[m, other])
                            sum_dist += (1 - correlation)

                        if sum_dist < min_sum_dist:
                            min_sum_dist = sum_dist
                            centroid = m
                else:
                    centroid = component[0]

                classes.append({
                    'Stocks': ", ".join(sorted([str(s) for s in component if s is not None])),
                    'Centroid': centroid,
                    'Lowest Correlation': min_corr,
                    'Size': len(component)
                })

        if classes:
            classes_df = pd.DataFrame(classes).sort_values(by='Lowest Correlation', ascending=False)
            st.write(f"Found **{len(classes)}** correlation classes.")
            st.dataframe(classes_df)

            if st.button("Save Classes to CSV"):
                output_name = f"correlation_classes_{threshold}.csv"
                classes_df.to_csv(output_name, index=False)
                st.success(f"Saved to {output_name}")
        else:
            st.write("No correlation classes found.")
    else:
        st.write("No pairs match the threshold, so no classes can be formed.")

if __name__ == "__main__":
    main()
