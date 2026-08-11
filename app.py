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

# Configuración de Centros y Depósitos Base
CENTROS_CONFIG = {
    "Córdoba (Centro 0960)": {
        "codigos": ["0960", "960"],
        "depot_address": "Depósito San Isidro EDASA Coca Cola X5016 Córdoba, Argentina",
        "depot_coords": (-31.442, -64.148),
        "provincia": "Córdoba",
        "viewbox": "-64.30,-31.50,-64.00,-31.30"
    },
    "Mendoza (Centro 0962)": {
        "codigos": ["0962", "962"],
        "depot_address": "Alsina 2336, M5501 Godoy Cruz, Mendoza, Argentina",
        "depot_coords": (-32.923, -68.835),
        "provincia": "Mendoza",
        "viewbox": "-68.95,-33.00,-68.75,-32.80"
    }
}

MINUTOS_POR_PARADA = 20
MAX_HORAS_JORNADA = 7.5

st.title("🚚 Torre de Control - Servicio Técnico Frío")
st.subheader("Ruteador Multirregión de Operaciones")

# 1. Cargar archivo Excel
uploaded_file = st.file_uploader("Cargar planilla de órdenes (.xlsx)", type=["xlsx"])

if uploaded_file is not None:
    try:
        # Leer el Excel sin asumir nombres fijos de cabecera si varían
        df = pd.read_excel(uploaded_file)
    except Exception as e:
        st.error(f"Error al leer el archivo Excel: {e}")
        st.stop()

    # Normalizar nombres de columnas a string
    df.columns = [str(c).strip() for c in df.columns]

    # Mapeo de columnas por índice o nombre estándar
    col_orden = df.columns[0]      # Columna A: Número de orden
    col_cliente = df.columns[2]    # Columna C: Nombre del Cliente
    col_direccion = df.columns[3]  # Columna D: Dirección del cliente
    col_telefono = df.columns[5]   # Columna F: Teléfono
    col_cp = df.columns[6]         # Columna G: Código Postal
    col_texto_breve = df.columns[7]# Columna H: Texto breve / Observaciones
    col_activo = df.columns[9]     # Columna J: Activo fijo
    col_centro = df.columns[10]    # Columna K: Centro

    # Filtro de región por parte del administrativo
    opcion_region = st.selectbox("Seleccione el Centro / Región a procesar:", list(CENTROS_CONFIG.keys()))
    config_actual = CENTROS_CONFIG[opcion_region]

    # Filtrar el dataframe por el centro seleccionado
    codigos_centro = [str(c) for c in config_actual["codigos"]]
    df_filtrado = df[df[col_centro].astype(str).str.strip().isin(codigos_centro)].copy()

    if df_filtrado.empty:
        st.warning(f"⚠️ No se encontraron órdenes correspondientes a {opcion_region} en este archivo.")
        st.stop()

    st.success(f"📊 Se encontraron **{len(df_filtrado)} órdenes** asociadas a **{opcion_region}**.")

    if st.button("⚡ Optimizar y Despachar Flota", type="primary"):
        with st.spinner(f"Geolocalizando paradas en {config_actual['provincia']} y calculando rutas óptimas..."):
            
            # Geolocalización combinando Dirección + CP
            def obtener_coords(dir_texto, cp_texto):
                try:
                    query = f"{dir_texto}, CP {cp_texto}, {config_actual['provincia']}, Argentina"
                    url = "https://nominatim.openstreetmap.org/search"
                    params = {
                        'q': query,
                        'format': 'json',
                        'limit': 1,
                        'viewbox': config_actual['viewbox'],
                        'bounded': 1
                    }
                    headers = {'User-Agent': 'RuteadorFrioMultiregion/1.0'}
                    response = requests.get(url, params=params, headers=headers, timeout=5)
                    data = response.json()
                    if data:
                        return float(data[0]['lat']), float(data[0]['lon'])
                except Exception:
                    pass
                return config_actual["depot_coords"]

            depot_lat, depot_lng = config_actual["depot_coords"]
            
            lats, lngs = [], []
            for idx, row in df_filtrado.iterrows():
                lat, lng = obtener_coords(row[col_direccion], row[col_cp])
                lats.append(lat)
                lngs.append(lng)

            df_filtrado['lat'] = lats
            df_filtrado['lng'] = lngs

            # Evaluación de división en vehículos
            total_ordenes = len(df_filtrado)
            usar_dos = total_ordenes >= 10

            if usar_dos:
                coords_matrix = df_filtrado[['lat', 'lng']].values
                kmeans = KMeans(n_clusters=2, random_state=42, n_init=10).fit(coords_matrix)
                df_filtrado['cluster'] = kmeans.labels_
                
                # Asignación de vehículos
                df_filtrado['Vehículo Asignado'] = df_filtrado['cluster'].map({0: 'Vehículo 1 (Zona A)', 1: 'Vehículo 2 (Zona B)'})
            else:
                df_filtrado['Vehículo Asignado'] = 'Vehículo 1'

            # Función de Ruteo Individual con OR-Tools
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
                secuencia_indices, distancia_total = [], 0

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

            vehiculos_unicos = df_filtrado['Vehículo Asignado'].unique()
            
            st.divider()
            st.info(f"💡 **DESPACHO REGIONAL ({config_actual['provincia'].upper()}):** Se asignaron **{len(vehiculos_unicos)} VEHÍCULO(S)** saliendo de `{config_actual['depot_address']}`.")

            cols = st.columns(len(vehiculos_unicos))
            origen_encoded = urllib.parse.quote(config_actual["depot_address"])

            for idx_col, v_nombre in enumerate(sorted(vehiculos_unicos)):
                grupo_df = df_filtrado[df_filtrado['Vehículo Asignado'] == v_nombre]
                
                sub_df_ordenado, km_v = optimizar_secuencia_grupo(grupo_df)
                
                direcciones_ordenadas = sub_df_ordenado[col_direccion].tolist()
                waypoints_encoded = "|".join([urllib.parse.quote(f"{d}, {config_actual['provincia']}") for d in direcciones_ordenadas])
                link_maps = f"https://www.google.com/maps/dir/?api=1&origin={origen_encoded}&destination={origen_encoded}&waypoints={waypoints_encoded}&travelmode=driving"
                
                tiempo_est = (km_v / 25.0) + ((len(sub_df_ordenado) * MINUTOS_POR_PARADA) / 60.0)

                with cols[idx_col]:
                    st.markdown(f"### 🚚 {v_nombre}")
                    st.metric("Paradas", len(sub_df_ordenado))
                    st.metric("Recorrido Est.", f"{km_v:.1f} km")
                    st.metric("Jornada Est.", f"{tiempo_est:.1f} hs")

                    st.markdown("**Secuencia Óptima:**")
                    paso = 1
                    texto_paradas_wa = ""
                    
                    for _, item in sub_df_ordenado.iterrows():
                        num_orden = str(item[col_orden]) if pd.notna(item[col_orden]) else "-"
                        nom_cliente = str(item[col_cliente]) if pd.notna(item[col_cliente]) else "Cliente"
                        dir_cliente = str(item[col_direccion]) if pd.notna(item[col_direccion]) else "-"
                        tel_cliente = str(item[col_telefono]) if pd.notna(item[col_telefono]) else "N/A"
                        txt_breve = str(item[col_texto_breve]) if pd.notna(item[col_texto_breve]) else "Sin notas"
                        activo_fijo = str(item[col_activo]) if pd.notna(item[col_activo]) else "N/A"

                        # Vista en la Web
                        st.write(f"**{paso}.** [{num_orden}] **{nom_cliente}** - {dir_cliente}")
                        
                        # Cadena de texto detallada para el WhatsApp del técnico
                        texto_paradas_wa += (
                            f"%0A*{paso}. Orden #{num_orden} - {nom_cliente}*%0A"
                            f"📍 Dirección: {dir_cliente}%0A"
                            f"📞 Teléfono: {tel_cliente}%0A"
                            f"🧊 Activo Fijo: {activo_fijo}%0A"
                            f"📝 Obs: {txt_breve}%0A"
                        )
                        paso += 1

                    st.link_button("🗺️ Abrir Hoja de Ruta en Google Maps", link_maps)
                    
                    # Mensaje estructurado de WhatsApp
                    msg_wa = (
                        f"🚚 *HOJA DE RUTA - {v_nombre.upper()} ({config_actual['provincia'].upper()})*%0A"
                        f"📍 *Punto de Salida/Retorno:* {config_actual['depot_address']}%0A"
                        f"📊 *Total de Visitas:* {len(sub_df_ordenado)} paradas%0A"
                        f"----------------------------------------%0A"
                        f"📋 *DETALLE DE PARADAS:*%0A"
                        f"{texto_paradas_wa}%0A"
                        f"----------------------------------------%0A"
                        f"🔗 *Link de Google Maps Ordenado:*%0A{urllib.parse.quote(link_maps)}"
                    )
                    
                    st.link_button("💬 Enviar por WhatsApp", f"https://api.whatsapp.com/send?text={msg_wa}")

                # Actualizar DataFrame original con los datos asignados
                for original_idx in sub_df_ordenado.index:
                    df.loc[original_idx, 'Vehículo Asignado'] = v_nombre
                    df.loc[original_idx, 'Estado'] = 'En Ruta'
                    df.loc[original_idx, 'Link de Ruta'] = str(link_maps)

            # Botón de Descargar Excel procesado
            st.divider()
            output_name = "ordenes_despachadas.xlsx"
            df.to_excel(output_name, index=False)
            
            with open(output_name, "rb") as file:
                st.download_button(
                    label="📥 Descargar Excel con Asignaciones y Links",
                    data=file,
                    file_name="ordenes_despachadas.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
