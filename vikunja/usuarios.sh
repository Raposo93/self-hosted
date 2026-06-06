#!/bin/bash

# Función para listar usuarios
listar_usuarios() {
    echo "Listado de usuarios:"
    docker exec vikunja_api /app/vikunja/vikunja user list
}

# Función para crear usuario
crear_usuario() {
    # Solicitar al usuario el nombre de usuario
    read -p "Introduce el nombre de usuario: " username

    # Solicitar al usuario la contraseña (sin mostrarla en pantalla)
    read -sp "Introduce la contraseña: " password
    echo

    # Solicitar al usuario el correo electrónico
    read -p "Introduce el correo electrónico: " email

    # Ejecutar el comando docker con los valores proporcionados por el usuario
    docker exec vikunja_api /app/vikunja/vikunja user create -e "$email" -p "$password" -u "$username"
}

# Función para borrar usuario
borrar_usuario() {
    # Listar usuarios para mostrar los IDs
    listar_usuarios

    # Solicitar al usuario el ID del usuario a eliminar
    read -p "Introduce el ID del usuario que deseas eliminar: " userID

    # Ejecutar el comando de docker para eliminar el usuario con el ID proporcionado
    docker exec vikunja_api /app/vikunja/vikunja user delete "$userID" -c -n
}

# Función principal
main() {
    while true; do
        echo "¿Qué quieres hacer?"
        echo "1. Listar usuarios"
        echo "2. Crear usuario"
        echo "3. Borrar usuario"
        echo "4. Salir"

        read -p "Introduce el número de la acción que deseas realizar: " opcion

        case $opcion in
            1)
                listar_usuarios
                ;;
            2)
                crear_usuario
                ;;
            3)
                borrar_usuario
                ;;
            4)
                echo "Saliendo del programa."
                exit 0
                ;;
            *)
                echo "Opción inválida. Por favor, selecciona una opción válida."
                ;;
        esac
    done
}

# Llamada a la función principal
main
