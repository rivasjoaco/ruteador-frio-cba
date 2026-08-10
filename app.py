import streamlit as st
import pandas as pd
import numpy as np
import urllib.parse
import requests
from sklearn.cluster import KMeans
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

st.set_page_config(
    page_title="Torre de Control - Servicio Técnico Frío",
    page_icon="🚚",
    layout="wide"
)

DEPOT_ADDRESS = "Depósito San Isidro EDASA Coca Cola, Córdoba, Argentina""
MINUTOS_POR_PARADA = 20
MAX_HORAS_JORNADA = 7.5

st.title("🚚 Torre de Control - Servicio Técnico Frío")
st.subheader("Depósito San Isidro (Córdoba)")

uploaded_file = st.file_uploader("Cargar archivo de órdenes (.xlsx)", type=["xlsx"])

if uploaded_file is not None:
    try:
        df = pd.read_excel(uploaded_file, sheet_name="Ordenes")
    except Exception as e:
        st.error(f"Error al leer la pestaña 'Ordenes': {e}")
        st.stop()

    if 'Link de Ruta' not in df.columns:
        df['Link de Ruta'] = ""
    else:
        df['Link de Ruta'] = df['Link de Ruta'].astype(str)

    pendientes = df[df['Estado'].astype(str).str.strip().str.upper() == 'PENDIENTE'].copy()

    if pendientes.empty:
        st.warning("⚠️ No hay órdenes pendientes en este archivo.")
        st.stop()

    st.success(f"📊 Se encontraron **{len(pendientes)} órdenes pendientes** para procesar.")

    if st.button("⚡ Optimizar y Despachar Flota", type="primary"):
        with st.spinner("Geolocalizando paradas, agrupando por zonas geográficas y ruteando con OR-Tools..."):
            
            # 1. Geolocalización
            def obtener_coords(direccion):
                try:
                    url = "https://nominatim.openstreetmap.org/search"
                    params = {
                        'q': f"{direccion}, Córdoba, Argentina",
                        'format': 'json',
                        'limit': 1,
                        'viewbox': '-64.30,-31.50,-64.00,-31.30',
                        'bounded': 1
                    }
                    headers = {'User-Agent': 'RuteadorFrioStreamlit/2.0'}
                    response = requests.get(url, params=params, headers=headers, timeout=5)
                    data = response.json()
                    if data:
                        return float(data[0]['lat']), float(data[0]['lon'])
                except Exception:
                    pass
                return -31.442, -64.148

            depot_lat, depot_lng = -31.442, -64.148
            
            lats, lngs = [], []
            for idx, row in pendientes.iterrows():
                lat, lng = obtener_coords(row['Dirección'])
                lats.append(lat)
                lngs.append(lng)

            pendientes['lat'] = lats
            pendientes['lng'] = lngs

            # 2. Evaluación de número de vehículos
            total_ordenes = len(pendientes)
            usar_dos = total_ordenes >= 10 # Si hay 10 o más órdenes, activa 2 zonas/vehículos

            # 3. Clustering Geográfico Previo (K-Means)
            if usar_dos:
                coords_matrix = pendientes[['lat', 'lng']].values
                kmeans = KMeans(n_clusters=2, random_state=42, n_init=10).fit(coords_matrix)
                
                # Asignar cluster 0 o 1
                pendientes['cluster'] = kmeans.labels_
                
                # Definir cuál es Zona Sur y cuál Zona Norte según la latitud media del cluster
                cluster_0_lat = pendientes[pendientes['cluster'] == 0]['lat'].mean()
                cluster_1_lat = pendientes[pendientes['cluster'] == 1]['lat'].mean()
                
                # En Córdoba, latitud más negativa = Más al Sur
                if cluster_0_lat < cluster_1_lat:
                    pendientes['Vehículo Asignado'] = pendientes['cluster'].map({0: 'Vehículo 1 (Zona Sur)', 1: 'Vehículo 2 (Zona Norte)'})
                else:
                    pendientes['Vehículo Asignado'] = pendientes['cluster'].map({1: 'Vehículo 1 (Zona Sur)', 0: 'Vehículo 2 (Zona Norte)'})
            else:
                pendientes['Vehículo Asignado'] = 'Vehículo 1'

            # 4. Función de Ruteo Individual con OR-Tools para cada grupo
            def optimizar_secuencia_grupo(df_grupo):
                coords_grupo = [(depot_lat, depot_lng)]
                for _, row in df_grupo.iterrows():
                    coords_grupo.append((row['lat'], row['lng']))

                n_locs = len(coords_grupo)
                dist_matrix = []

                for i in range(n_locs):
                    row_dist = []
                    for j in range(n_locs):
                        if i == j:
                            row_dist.append(0)
                        else:
                            lat1, lon1 = np.radians(coords_grupo[i][0]), np.radians(coords_grupo[i][1])
                            lat2, lon2 = np.radians(coords_grupo[j][0]), np.radians(coords_grupo[j][1])
                            dlat, dlon = lat2 - lat1, lon2 - lon1
                            a = np.sin(dlat/2)**2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon/2)**2
                            c = 2 * np.arcsin(np.sqrt(a))
                            dist_meters = int(c * 6371000 * 1.35)
                            row_dist.append(dist_meters)
                    dist_matrix.append(row_dist)

                manager = pywrapcp.RoutingIndexManager(n_locs, 1, 0)
                routing = pywrapcp.RoutingModel(manager)

                def distance_callback(from_index, to_index):
                    from_node = manager.IndexToNode(from_index)
                    to_node = manager.IndexToNode(to_index)
                    return int(dist_matrix[from_node][to_node])

                transit_callback_index = routing.RegisterTransitCallback(distance_callback)
                routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

                search_parameters = pywrapcp.DefaultRoutingSearchParameters()
                search_parameters.first_solution_strategy = (
                    routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
                )

                solution = routing.SolveWithParameters(search_parameters)
                
                secuencia_indices = []
                distancia_total = 0

                if solution:
                    index = routing.Start(0)
                    while not routing.IsEnd(index):
                        node = manager.IndexToNode(index)
                        if node != 0:
                            secuencia_indices.append(node - 1)
                        prev_index = index
                        index = solution.Value(routing.NextVar(index))
                        distancia_total += routing.GetArcCostForVehicle(prev_index, index, 0)

                return df_grupo.iloc[secuencia_indices].copy(), distancia_total / 1000.0

            # 5. Ruteo por vehículo
            vehiculos_unicos = pendientes['Vehículo Asignado'].unique()
            
            st.divider()
            st.info(f"💡 **RECOMENDACIÓN DE FLOTA:** Conviene despachar **{len(vehiculos_unicos)} VEHÍCULO(S)** divididos por zonas geográficas puras.")

            cols = st.columns(len(vehiculos_unicos))
            origen_encoded = urllib.parse.quote(DEPOT_ADDRESS)

            for idx_col, v_nombre in enumerate(sorted(vehiculos_unicos)):
                grupo_df = pendientes[pendientes['Vehículo Asignado'] == v_nombre]
                
                # Optimizar secuencia con OR-Tools
                sub_df_ordenado, km_v = optimizar_secuencia_grupo(grupo_df)
                
                direcciones_ordenadas = sub_df_ordenado['Dirección'].tolist()
                waypoints_encoded = "|".join([urllib.parse.quote(d) for d in direcciones_ordenadas])
                link_maps = f"https://www.google.com/maps/dir/?api=1&origin={origen_encoded}&destination={origen_encoded}&waypoints={waypoints_encoded}&travelmode=driving"
                
                tiempo_est = (km_v / 25.0) + ((len(sub_df_ordenado) * MINUTOS_POR_PARADA) / 60.0)

                with cols[idx_col]:
                    st.markdown(f"### 🚚 {v_nombre}")
                    st.metric("Paradas", len(sub_df_ordenado))
                    st.metric("Recorrido Est.", f"{km_v:.1f} km")
                    st.metric("Jornada Est.", f"{tiempo_est:.1f} hs")

                    st.markdown("**Secuencia Óptima:**")
                    paso = 1
                    for _, item in sub_df_ordenado.iterrows():
                        st.write(f"**{paso}.** [{item['ID Orden']}] {item['Cliente / Local']} - *{item['Prioridad']}*")
                        paso += 1

                    st.link_button("🗺️ Abrir Hoja de Ruta en Google Maps", link_maps)
                    
                    msg_wa = f"Hola! Hoja de Ruta para {v_nombre} (Depósito San Isidro):%0A%0ALink Google Maps:%0A{urllib.parse.quote(link_maps)}"
                    st.link_button("💬 Enviar por WhatsApp", f"https://api.whatsapp.com/send?text={msg_wa}")

                # Actualizar DataFrame principal
                for original_idx in sub_df_ordenado.index:
                    df.loc[original_idx, 'Vehículo Asignado'] = v_nombre
                    df.loc[original_idx, 'Estado'] = 'En Ruta'
                    df.loc[original_idx, 'Link de Ruta'] = str(link_maps)

            # Botón de Descargar
            st.divider()
            output_name = "ordenes_despachadas.xlsx"
            df.to_excel(output_name, sheet_name="Ordenes", index=False)
            
            with open(output_name, "rb") as file:
                st.download_button(
                    label="📥 Descargar Excel con Estados y Links Actualizados",
                    data=file,
                    file_name="ordenes_despachadas.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
